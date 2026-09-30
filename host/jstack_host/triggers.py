"""The environment's one mechanism: a trigger's condition is met → an action lands.

A trigger is data — `{id, events, engines, condition, action, dedup}` — and every
environment feature is one of them: compact-on-delivery, and in time session
settings, path rules, the context meter. One dispatcher hook per event reads the
registry, so adding a trigger never edits `hooks.json`; Codex keys hook trust by an
entry's position, and every new entry there used to disarm Codex in silence.

CONDITIONS are built-in (a Python callable named in `BUILTINS`) or a SCRIPT: the
normalized event arrives as JSON on stdin, exit 0 means met, a JSON object on stdout
may carry `text`, `keys` or a dedup `key`. A script that errors or overruns its
timeout is NOT MET, and the error is logged — a broken condition never breaks a turn.

ACTIONS:
  inject — text the model reads: `additionalContext` where the event carries it, the
           block reason on Stop (the only channel that event has).
  block  — PreToolUse deny, with the text as the reason.
  input  — keys into the session's managed pane, run detached by an executor
           (`EXECUTORS`) that settles the row's outcome when delivery is done.

THE FIRE LOG (`triggers.jsonl` in the host's state dir) holds one row per evaluation
that matched an event, and one `settle` row per outcome, joined by `fire`. It is the
system's health: a fire with no outcome is a defect, not a success. The session
timeline reads it, which is how a fire reaches the jRemote sidebar.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import uuid
from pathlib import Path

from . import hostenv

EVENTS = ("SessionStart", "UserPromptSubmit", "PreToolUse", "PostToolUse", "Stop",
          "PreCompact", "SessionEnd")
ENGINES = ("claude", "codex")
ACTIONS = ("inject", "block", "input")
DEDUPS = ("always", "session", "key")
SCRIPT_TIMEOUT = 5.0
# Events whose hook output carries text the model reads as `additionalContext`.
CONTEXT_EVENTS = ("SessionStart", "UserPromptSubmit", "PreToolUse", "PostToolUse")


# --- where things live ---------------------------------------------------------------

def log_path() -> Path:
    raw = os.environ.get("JSTACK_TRIGGER_LOG")
    return Path(raw) if raw else hostenv.state_dir() / "triggers.jsonl"


def triggers_dir() -> Path:
    """External triggers: one JSON file each. The shell stub reads `.armed` here too,
    so it lives at a path a shell can name without asking the host."""
    raw = os.environ.get("JSTACK_TRIGGERS_DIR")
    return Path(raw).expanduser() if raw else Path.home() / ".config" / "jstack" / "triggers"


def dedup_path(sid: str) -> Path:
    return hostenv.state_dir() / "triggers-seen" / f"{sid}.json"


# --- the registry --------------------------------------------------------------------

class Invalid(ValueError):
    pass


def validate(t: dict, origin: str = "") -> dict:
    where = f"{origin}: " if origin else ""
    tid = t.get("id")
    if not tid or not isinstance(tid, str):
        raise Invalid(f"{where}a trigger needs an id")
    events = t.get("events")
    if not events or not set(events) <= set(EVENTS):
        raise Invalid(f"{where}{tid}: events must be a non-empty subset of {EVENTS}")
    engines = t.get("engines") or list(ENGINES)
    if not set(engines) <= set(ENGINES):
        raise Invalid(f"{where}{tid}: engines must be a subset of {ENGINES}")
    cond = t.get("condition")
    if not isinstance(cond, dict) or not (("builtin" in cond) ^ ("script" in cond)):
        raise Invalid(f"{where}{tid}: condition is {{builtin: name}} or {{script: path}}")
    if "builtin" in cond and cond["builtin"] not in BUILTINS:
        raise Invalid(f"{where}{tid}: no built-in condition {cond['builtin']!r}")
    action = t.get("action")
    if action not in ACTIONS:
        raise Invalid(f"{where}{tid}: action must be one of {ACTIONS}")
    if action == "input" and t.get("executor") not in EXECUTORS:
        raise Invalid(f"{where}{tid}: an input action names its executor, one of {sorted(EXECUTORS)}")
    if action == "block" and set(events) - {"PreToolUse"}:
        raise Invalid(f"{where}{tid}: block exists only on PreToolUse")
    dedup = t.get("dedup", "always")
    if dedup not in DEDUPS:
        raise Invalid(f"{where}{tid}: dedup must be one of {DEDUPS}")
    return {**t, "engines": list(engines), "dedup": dedup}


def registry() -> tuple[list[dict], list[str]]:
    """(triggers, problems). Built-ins first, then every `*.json` in the triggers dir.
    A file that does not validate is reported and skipped — never half-loaded."""
    found, problems = [validate(t, "builtin") for t in BUILTIN_TRIGGERS], []
    d = triggers_dir()
    if d.is_dir():
        for f in sorted(d.glob("*.json")):
            try:
                t = validate(json.loads(f.read_text()), f.name)
            except (Invalid, ValueError, OSError) as exc:
                problems.append(f"{f.name}: {exc}")
                continue
            if t.get("enabled", True) is False:
                continue
            if any(o["id"] == t["id"] for o in found):
                problems.append(f"{f.name}: id {t['id']!r} is already registered")
                continue
            found.append({**t, "origin": str(f)})
    return found, problems


def arm(triggers: list[dict] | None = None) -> set[str]:
    """Write the events that have a trigger, so the hook stub can skip the host for
    every event nothing listens to. Written on every dispatch; `trigger arm` forces it."""
    if triggers is None:
        triggers, _ = registry()
    events = {e for t in triggers for e in t["events"]}
    d = triggers_dir()
    try:
        d.mkdir(parents=True, exist_ok=True)
        body = "".join(f"{e}\n" for e in sorted(events))
        path = d / ".armed"
        if not path.exists() or path.read_text() != body:
            tmp = d / f".armed.{os.getpid()}"
            tmp.write_text(body)
            tmp.replace(path)
    except OSError:
        pass
    return events


# --- the event -----------------------------------------------------------------------

def normalize(payload: dict, event: str) -> dict:
    """One shape for both engines. The engine is read off the transcript itself —
    a Codex rollout and a Claude transcript differ on their first lines."""
    path = payload.get("transcript_path") or ""
    engine = "claude"
    if path and os.path.isfile(path):
        from . import compact_delivery
        engine = compact_delivery.engine_of(path)
    return {
        "event": event,
        "engine": engine,
        "sid": payload.get("session_id") or "",
        "transcript": path,
        "cwd": payload.get("cwd") or "",
        "tool": payload.get("tool_name") or "",
        "tool_input": payload.get("tool_input") or {},
        "prompt": payload.get("prompt") or "",
        "source": payload.get("source") or "",
        "stop_hook_active": bool(payload.get("stop_hook_active")),
        "raw": payload,
    }


# --- conditions ----------------------------------------------------------------------

def run_script(spec: dict, ev: dict, trigger: dict) -> dict:
    """{met, text?, keys?, key?, error?} — never raises."""
    script = os.path.expanduser(spec["script"])
    if not os.path.isabs(script) and trigger.get("origin"):
        script = str(Path(trigger["origin"]).parent / script)
    timeout = float(spec.get("timeout", SCRIPT_TIMEOUT))
    body = json.dumps({k: v for k, v in ev.items() if k != "raw"} | {"payload": ev["raw"]})
    try:
        r = subprocess.run([script], input=body, capture_output=True, text=True,
                           timeout=timeout, env={**os.environ, "JSTACK_TRIGGER": trigger["id"]})
    except subprocess.TimeoutExpired:
        return {"met": False, "error": f"timed out after {timeout:g}s"}
    except OSError as exc:
        return {"met": False, "error": f"cannot run {script}: {exc.strerror or exc}"}
    if r.returncode not in (0, 1):
        tail = (r.stderr or "").strip().splitlines()[-1:] or [""]
        return {"met": False, "error": f"exit {r.returncode}: {tail[0][:200]}"}
    out: dict = {"met": r.returncode == 0}
    if r.returncode == 0 and r.stdout.strip():
        try:
            extra = json.loads(r.stdout)
        except ValueError:
            extra = {"text": r.stdout.strip()}
        if isinstance(extra, dict):
            out.update({k: extra[k] for k in ("text", "keys", "key") if k in extra})
    return out


def evaluate_one(t: dict, ev: dict) -> dict:
    cond = t["condition"]
    try:
        if "builtin" in cond:
            res = BUILTINS[cond["builtin"]](ev, t)
        else:
            res = run_script(cond, ev, t)
    except Exception as exc:  # a broken condition is not met, and says why
        res = {"met": False, "error": f"{type(exc).__name__}: {exc}"}
    if res.get("met") and "text" not in res and t.get("text"):
        res["text"] = t["text"]
    return res


# --- dedup ---------------------------------------------------------------------------

def _seen(sid: str) -> dict:
    try:
        return json.loads(dedup_path(sid).read_text())
    except (OSError, ValueError):
        return {}


def already(t: dict, sid: str, key: str | None) -> bool:
    if t["dedup"] == "always" or not sid:
        return False
    seen = _seen(sid).get(t["id"], [])
    return bool(seen) if t["dedup"] == "session" else (key or "") in seen


def mark(t: dict, sid: str, key: str | None) -> None:
    if t["dedup"] == "always" or not sid:
        return
    path = dedup_path(sid)
    seen = _seen(sid)
    seen.setdefault(t["id"], []).append(key or "")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(seen))
    except OSError:
        pass


# --- the log -------------------------------------------------------------------------

def _append(row: dict) -> None:
    try:
        path = log_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a") as fh:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    except OSError:
        pass


def record(**row) -> dict:
    row = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), **row}
    _append(row)
    return row


def settle(fire: str, outcome: str, **fields) -> None:
    """The outcome of an action that finishes after the hook returned."""
    if fire:
        record(settle=fire, outcome=outcome, **fields)


def fires(sid: str | None = None, since: str | None = None) -> list[dict]:
    """Every fire row with its latest outcome folded in, oldest first. `sid` matches the
    full id or its first eight characters, the way the older logs key sessions."""
    try:
        lines = log_path().read_text().splitlines()
    except OSError:
        return []
    rows, by_fire = [], {}
    for ln in lines:
        try:
            r = json.loads(ln)
        except ValueError:
            continue
        if "settle" in r:
            f = by_fire.get(r["settle"])
            if f is not None:
                f.setdefault("settled", []).append({k: v for k, v in r.items() if k != "settle"})
                f["outcome"] = r.get("outcome")
            continue
        if sid and not (r.get("sid") == sid or str(r.get("sid", ""))[:8] == sid[:8]):
            continue
        if since and r.get("ts", "") < since:
            continue
        rows.append(r)
        if r.get("fire"):
            by_fire[r["fire"]] = r
    return rows


# --- dispatch ------------------------------------------------------------------------

def dispatch(event: str, payload: dict) -> dict | None:
    """Evaluate every trigger on this event and engine; act; log. Returns the hook's
    stdout object, or None when nothing is to be said."""
    triggers, problems = registry()
    arm(triggers)
    ev = normalize(payload, event)
    for p in problems:
        record(sid=ev["sid"], engine=ev["engine"], event=event, trigger=None,
               met=False, fired=False, error=f"registry: {p}")
    texts, deny = [], []
    for t in triggers:
        if event not in t["events"] or ev["engine"] not in t["engines"]:
            continue
        if t.get("tools") and ev["tool"] not in t["tools"]:
            continue
        res = evaluate_one(t, ev)
        met = bool(res.get("met"))
        dup = met and already(t, ev["sid"], res.get("key"))
        fired = met and not dup
        row = {"sid": ev["sid"], "engine": ev["engine"], "event": event, "trigger": t["id"],
               "met": met, "fired": fired, "action": t["action"]}
        for k in ("text", "keys", "key", "error", "reason"):
            if res.get(k):
                row[k] = res[k]
        if dup:
            row["reason"] = f"already fired this {'session' if t['dedup'] == 'session' else 'key'}"
        if fired:
            row["fire"] = uuid.uuid4().hex[:12]
            mark(t, ev["sid"], res.get("key"))
            if t["action"] == "inject" and res.get("text"):
                texts.append(res["text"])
                row["outcome"] = "delivered"
            elif t["action"] == "block":
                deny.append(res.get("text") or f"blocked by trigger {t['id']}")
                row["outcome"] = "delivered"
            elif t["action"] == "input":
                row["outcome"] = "pending"
        record(**row)
        if fired and t["action"] == "input":
            try:
                EXECUTORS[t["executor"]](ev, res, row["fire"])
            except Exception as exc:
                settle(row["fire"], "error", error=f"{type(exc).__name__}: {exc}")
    return render(event, texts, deny)


def render(event: str, texts: list[str], deny: list[str]) -> dict | None:
    if deny and event == "PreToolUse":
        return {"hookSpecificOutput": {"hookEventName": "PreToolUse",
                                       "permissionDecision": "deny",
                                       "permissionDecisionReason": "\n\n".join(deny)}}
    if not texts:
        return None
    text = "\n\n".join(texts)
    if event in CONTEXT_EVENTS:
        return {"hookSpecificOutput": {"hookEventName": event, "additionalContext": text}}
    if event == "Stop":
        return {"decision": "block", "reason": text}
    return None  # no channel on this event reaches the model


def main(argv: list[str]) -> int:
    """`jstack-host trigger dispatch <Event>` — the hook. Always exit 0: a dispatcher
    that fails is every trigger switched off, so failure goes to the log, not the CLI."""
    event = argv[0] if argv else ""
    try:
        payload = json.load(sys.stdin)
    except Exception:
        payload = {}
    if event not in EVENTS:
        record(event=event, trigger=None, met=False, fired=False, error="unknown event")
        return 0
    try:
        out = dispatch(event, payload if isinstance(payload, dict) else {})
    except Exception as exc:
        record(event=event, sid=(payload or {}).get("session_id", ""), trigger=None,
               met=False, fired=False, error=f"dispatch: {type(exc).__name__}: {exc}")
        return 0
    if out:
        sys.stdout.write(json.dumps(out))
    return 0


USAGE = """jstack-host trigger dispatch <Event>      the hook: payload on stdin, hook JSON on stdout
jstack-host trigger list                  every registered trigger, and any file that failed to load
jstack-host trigger arm                   rewrite the armed-events file the hook stub reads
jstack-host trigger log [--sid S] [--json] [-n N]   fires, newest last, each with its outcome"""


def cli(argv: list[str]) -> int:
    verb, rest = (argv[0] if argv else ""), argv[1:]
    if verb == "dispatch":
        return main(rest)
    if verb == "arm":
        print(" ".join(sorted(arm())) or "(no events)")
        return 0
    if verb == "list":
        found, problems = registry()
        for t in found:
            cond = t["condition"].get("builtin") or t["condition"].get("script")
            print(f"{t['id']}\t{','.join(t['events'])}\t{','.join(t['engines'])}\t"
                  f"{t['action']}\t{cond}\t{t.get('origin', 'builtin')}")
        for p in problems:
            print(f"INVALID {p}", file=sys.stderr)
        return 1 if problems else 0
    if verb == "log":
        sid, as_json, n = None, False, 20
        i = 0
        while i < len(rest):
            if rest[i] == "--sid":
                sid, i = rest[i + 1], i + 2
            elif rest[i] == "--json":
                as_json, i = True, i + 1
            elif rest[i] == "-n":
                n, i = int(rest[i + 1]), i + 2
            else:
                print(USAGE, file=sys.stderr)
                return 2
        rows = [r for r in fires(sid) if r.get("met") or r.get("error")][-n:]
        for r in rows:
            if as_json:
                print(json.dumps(r, ensure_ascii=False))
            else:
                what = r.get("text") or r.get("keys") or r.get("error") or r.get("reason") or ""
                print(f"{r.get('ts')} {str(r.get('sid'))[:8]} {r.get('engine')} {r.get('event')} "
                      f"{r.get('trigger')} fired={r.get('fired')} {r.get('action')} "
                      f"outcome={r.get('outcome', '-')} | {str(what)[:100]}")
        return 0
    print(USAGE, file=sys.stderr)
    return 2


# --- built-in triggers ---------------------------------------------------------------

def _compact_condition(ev: dict, t: dict) -> dict:
    """Met when a turn ended where compact-on-delivery has something to decide. The
    closing message — the to-be-continued mark — is written only after Stop returns, so
    the declaration and the weight are settled by the detached child, whose verdict
    comes back to this row as its outcome."""
    from . import compact_delivery as cd
    ok, detail = cd.precheck(ev["raw"])
    if not ok:
        return {"met": False, "reason": detail}
    return {"met": True, "handoff": detail, "keys": "/compact (when the child decides)"}


def _compact_executor(ev: dict, res: dict, fire: str) -> None:
    from . import compact_delivery as cd
    cd.spawn_child({**res["handoff"], "fire": fire})


BUILTINS = {"compact-on-delivery": _compact_condition}
EXECUTORS = {"compact-delivery": _compact_executor}

BUILTIN_TRIGGERS = [
    {"id": "compact-on-delivery", "events": ["Stop"], "engines": ["claude", "codex"],
     "condition": {"builtin": "compact-on-delivery"}, "action": "input",
     "executor": "compact-delivery", "dedup": "always",
     "about": "A turn ends at a seam — heavy, or closed with the to-be-continued mark — "
              "in a managed pane: type /compact, then the resume."},
]
