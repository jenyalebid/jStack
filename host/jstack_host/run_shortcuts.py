"""Run shortcuts: operator-authored scripts the app can start with a few options.

A shortcut is a folder in `hostenv.shortcuts_dir()` — `shortcut.json` plus an
executable. The host owns everything about it: the catalog, every option's
allowed values, validation, the run, its output, and the push when it ends.
The app only renders what it is handed.

A run lives in its own tmux session on the managed socket, so it outlives a
host restart; its record is a directory under `state_dir()/shortcut-runs/`.
Named outside `jr-` so `managed.reconcile()` never reaps it.

Integrity is ownership: a manifest, script or source that is not owned by this
user, or is writable by anyone else, is refused — the same bar `ssh` holds.
Contract and authoring: the `Hub/shortcuts` system (`systems.json`).
"""
from __future__ import annotations

import json
import os
import re
import secrets
import shlex
import stat
import subprocess
import threading
import time
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse

from . import audit, hostenv, managed

MANIFEST = "shortcut.json"
SESSION_PREFIX = "shortcut-"
DEFAULT_TIMEOUT = 3600
SOURCE_TIMEOUT = 15
KEEP_RUNS = 200
OUTPUT_CHUNK = 256 * 1024
TEXT_MAX = 2000
OPTION_TYPES = ("choice", "toggle", "text")
FINAL = ("succeeded", "failed", "cancelled", "lost")

_ID = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
_OPT_ID = re.compile(r"^[a-z][a-z0-9_]{0,31}$")
_ANSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(\x07|\x1b\\)|\r(?!\n)")
_watch_lock = threading.Lock()
_watching = False

router = APIRouter()


class ShortcutError(ValueError):
    """A request the shortcut cannot honour — the 400 the app shows verbatim."""


def runs_dir() -> Path:
    return hostenv.state_dir() / "shortcut-runs"


# -- the catalog ------------------------------------------------------------

def _owned_and_private(path: Path) -> str:
    """'' when `path` is ours and nobody else can write it, else why not."""
    try:
        st = path.stat()
    except OSError as e:
        return f"{path.name}: {e.strerror or e}"
    if st.st_uid != os.getuid():
        return f"{path.name} is not owned by this user"
    if st.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
        return f"{path.name} is writable by other users"
    return ""


def _executable(folder: Path, rel: str, what: str) -> Path:
    if not isinstance(rel, str) or not rel or rel.startswith("/") or ".." in Path(rel).parts:
        raise ShortcutError(f"{what} must be a file inside the shortcut's folder")
    path = folder / rel
    if not path.is_file():
        raise ShortcutError(f"{what} {rel} does not exist")
    if not os.access(path, os.X_OK):
        raise ShortcutError(f"{what} {rel} is not executable")
    why = _owned_and_private(path)
    if why:
        raise ShortcutError(why)
    return path


def _script_env(extra: dict[str, str] | None = None) -> dict[str, str]:
    env = managed._server_env()
    env.pop("TMUX", None)
    env.update(extra or {})
    return env


def _resolve_source(folder: Path, rel: str) -> list[dict]:
    """A source prints the choices: a JSON list (strings or {value, label}),
    or one per line, `value<TAB>label`."""
    path = _executable(folder, rel, "source")
    try:
        r = subprocess.run([str(path)], cwd=folder, capture_output=True, text=True,
                           timeout=SOURCE_TIMEOUT, env=_script_env())
    except subprocess.TimeoutExpired:
        raise ShortcutError(f"source {rel} took longer than {SOURCE_TIMEOUT}s")
    if r.returncode != 0:
        tail = (r.stderr or r.stdout).strip().splitlines()[-1:] or [""]
        raise ShortcutError(f"source {rel} exited {r.returncode}: {tail[0][:200]}")
    out = r.stdout.strip()
    try:
        raw = json.loads(out) if out.startswith("[") else None
    except json.JSONDecodeError as e:
        raise ShortcutError(f"source {rel} printed bad JSON: {e}")
    if raw is None:
        raw = []
        for line in out.splitlines():
            value, _, label = line.partition("\t")
            if value.strip():
                raw.append({"value": value.strip(), "label": label.strip()})
    return _values(raw, f"source {rel}")


def _values(raw, where: str) -> list[dict]:
    if not isinstance(raw, list):
        raise ShortcutError(f"{where}: values must be a list")
    seen, values = set(), []
    for item in raw:
        if isinstance(item, str):
            item = {"value": item}
        if not isinstance(item, dict) or not isinstance(item.get("value"), str) or not item["value"]:
            raise ShortcutError(f"{where}: every value needs a non-empty string `value`")
        if item["value"] in seen:
            continue
        seen.add(item["value"])
        values.append({"value": item["value"],
                       "label": str(item.get("label") or item["value"])})
    return values


def _option(folder: Path, raw) -> dict:
    if not isinstance(raw, dict):
        raise ShortcutError("every option must be an object")
    oid = raw.get("id")
    if not isinstance(oid, str) or not _OPT_ID.match(oid):
        raise ShortcutError(f"option id {oid!r} must match {_OPT_ID.pattern}")
    kind = raw.get("type")
    if kind not in OPTION_TYPES:
        raise ShortcutError(f"option {oid}: type must be one of {', '.join(OPTION_TYPES)}")
    opt = {"id": oid, "label": str(raw.get("label") or oid), "type": kind,
           "help": str(raw.get("help") or ""), "placeholder": str(raw.get("placeholder") or "")}
    if kind == "choice":
        if "source" in raw:
            opt["values"] = _resolve_source(folder, raw["source"])
        else:
            opt["values"] = _values(raw.get("values"), f"option {oid}")
        allowed = [v["value"] for v in opt["values"]]
        default = raw.get("default")
        opt["default"] = default if default in allowed else (allowed[0] if allowed else "")
    elif kind == "toggle":
        opt["default"] = bool(raw.get("default", False))
    else:
        opt["default"] = str(raw.get("default") or "")
    return opt


def load(shortcut_id: str) -> dict:
    """One shortcut, options resolved. Raises KeyError for no such folder and
    ShortcutError for a broken one."""
    if not _ID.match(shortcut_id):
        raise KeyError(shortcut_id)
    folder = hostenv.shortcuts_dir() / shortcut_id
    manifest = folder / MANIFEST
    if not manifest.is_file():
        raise KeyError(shortcut_id)
    why = _owned_and_private(manifest) or _owned_and_private(folder)
    if why:
        raise ShortcutError(why)
    try:
        raw = json.loads(manifest.read_text())
    except (OSError, json.JSONDecodeError) as e:
        raise ShortcutError(f"{MANIFEST}: {e}")
    if not isinstance(raw, dict):
        raise ShortcutError(f"{MANIFEST} must be an object")
    options = raw.get("options") or []
    if not isinstance(options, list):
        raise ShortcutError("options must be a list")
    resolved = [_option(folder, o) for o in options]
    ids = [o["id"] for o in resolved]
    if len(ids) != len(set(ids)):
        raise ShortcutError("option ids must be unique")
    timeout = raw.get("timeout", DEFAULT_TIMEOUT)
    if not isinstance(timeout, int) or timeout <= 0:
        raise ShortcutError("timeout must be a positive number of seconds")
    return {
        "id": shortcut_id,
        "name": str(raw.get("name") or shortcut_id),
        "symbol": str(raw.get("symbol") or "bolt.fill"),
        "description": str(raw.get("description") or ""),
        "options": resolved,
        "timeout": timeout,
        "concurrent": bool(raw.get("concurrent", False)),
        "script": str(_executable(folder, raw.get("run"), "run")),
        "folder": str(folder),
    }


def catalog() -> list[dict]:
    """Every shortcut, a broken one included with its `error` — a manifest
    that fails to parse is an entry the operator needs to see, not a gap."""
    root = hostenv.shortcuts_dir()
    out = []
    for folder in sorted(p for p in root.iterdir() if p.is_dir()) if root.is_dir() else []:
        if not _ID.match(folder.name) or not (folder / MANIFEST).is_file():
            continue
        try:
            entry, error = load(folder.name), ""
        except ShortcutError as e:
            entry, error = _bare(folder), str(e)
        out.append({**_public(entry), "error": error,
                    "last_run": next(iter(runs(folder.name, 1)), None)})
    return sorted(out, key=lambda s: s["name"].lower())


def _bare(folder: Path) -> dict:
    try:
        raw = json.loads((folder / MANIFEST).read_text())
        raw = raw if isinstance(raw, dict) else {}
    except (OSError, json.JSONDecodeError):
        raw = {}
    return {"id": folder.name, "name": str(raw.get("name") or folder.name),
            "symbol": str(raw.get("symbol") or "exclamationmark.triangle"),
            "description": str(raw.get("description") or ""), "options": []}


def _public(entry: dict) -> dict:
    return {k: entry[k] for k in ("id", "name", "symbol", "description", "options")}


def validate(shortcut: dict, given) -> dict:
    """The options a run gets: every declared one, defaults filled in, each
    value checked against what this host offers right now."""
    if given is None:
        given = {}
    if not isinstance(given, dict):
        raise ShortcutError("options must be an object")
    declared = {o["id"]: o for o in shortcut["options"]}
    unknown = sorted(set(given) - set(declared))
    if unknown:
        raise ShortcutError(f"unknown option {', '.join(unknown)}")
    out = {}
    for oid, opt in declared.items():
        value = given.get(oid, opt["default"])
        if opt["type"] == "toggle":
            if not isinstance(value, bool):
                raise ShortcutError(f"{opt['label']} must be on or off")
        elif not isinstance(value, str):
            raise ShortcutError(f"{opt['label']} must be text")
        elif opt["type"] == "choice":
            if value not in {v["value"] for v in opt["values"]}:
                raise ShortcutError(f"{opt['label']}: {value!r} is not one of the choices")
        elif len(value) > TEXT_MAX or "\x00" in value:
            raise ShortcutError(f"{opt['label']} is too long")
        out[oid] = value
    return out


# -- runs -------------------------------------------------------------------

def _rundir(run_id: str) -> Path:
    if not re.match(r"^r-\d{8}-\d{6}-[0-9a-f]{4}$", run_id or ""):
        raise KeyError(run_id)
    return runs_dir() / run_id


def _read(path: Path) -> dict:
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return {}


def _write(path: Path, data: dict) -> None:
    # A temp name per writer: the watcher thread and a poll can write at once.
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    tmp.write_text(json.dumps(data, indent=2))
    tmp.replace(path)


def _session_alive(run_id: str) -> bool:
    return subprocess.run(managed._t("has-session", "-t", f"={SESSION_PREFIX}{run_id}"),
                          capture_output=True).returncode == 0


def _settle(rd: Path, rec: dict) -> dict:
    """Bring a record up to what the disk says, finalising it once."""
    if rec.get("state") != "running":
        return rec
    exit_file = rd / "exit"
    if exit_file.exists():
        try:
            code = int(exit_file.read_text().strip())
        except ValueError:
            code = -1
        result = _read(rd / "result.json")
        if (rd / "cancelled").exists():
            state = "cancelled"
        elif code == 0:
            state = "succeeded"
        else:
            state = "failed"
        summary = str(result.get("summary") or "")[:300]
        if not summary and (rd / "timed_out").exists():
            summary = f"Stopped after {rec.get('timeout', DEFAULT_TIMEOUT)}s"
        rec.update(state=state, exit_code=code, summary=summary,
                   url=str(result.get("url") or "")[:2000],
                   ended_at=exit_file.stat().st_mtime)
    elif not _session_alive(rec["id"]) and time.time() - rec.get("started_at", 0) > 5:
        rec.update(state="lost", ended_at=time.time(),
                   summary="The run ended without recording an exit code")
    else:
        return rec
    # Every reader settles, so the watcher, a poll and another process can all
    # reach here for one run; the exclusive marker lets exactly one finalise.
    try:
        os.close(os.open(rd / "settled", os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600))
    except FileExistsError:
        return _read(rd / "run.json") or rec
    _write(rd / "run.json", rec)
    _announce(rec)
    return rec


def _public_run(rec: dict) -> dict:
    keys = ("id", "shortcut", "name", "symbol", "options", "device", "state",
            "exit_code", "started_at", "ended_at", "summary", "url")
    return {k: rec.get(k) for k in keys}


def get_run(run_id: str) -> dict:
    rd = _rundir(run_id)
    rec = _read(rd / "run.json")
    if not rec:
        raise KeyError(run_id)
    return _settle(rd, rec)


def runs(shortcut_id: str = "", limit: int = 20) -> list[dict]:
    root = runs_dir()
    if not root.is_dir():
        return []
    out = []
    for rd in sorted(root.iterdir(), reverse=True):  # ids sort by start time
        rec = _read(rd / "run.json")
        if not rec or (shortcut_id and rec.get("shortcut") != shortcut_id):
            continue
        out.append(_public_run(_settle(rd, rec)))
        if len(out) >= limit:
            break
    return out


def _wrapper(rec: dict, rd: Path, env: dict[str, str]) -> str:
    """The pane's whole program. `set -m` puts the script in its own process
    group, so a cancel or a timeout takes its children (xcodebuild, a
    simulator) down with it rather than the shell alone."""
    exports = "\n".join(f"export {k}={shlex.quote(v)}" for k, v in sorted(env.items()))
    q = shlex.quote
    return f"""#!/bin/bash
{exports}
cd {q(rec['folder'])} || {{ echo 127 > {q(str(rd / 'exit'))}; exit; }}
set -m
{q(rec['script'])} >{q(str(rd / 'output.log'))} 2>&1 </dev/null &
pid=$!
echo $pid > {q(str(rd / 'pid'))}
( sleep {int(rec['timeout'])}; : > {q(str(rd / 'timed_out'))}; kill -TERM -- -$pid 2>/dev/null;
  sleep 10; kill -KILL -- -$pid 2>/dev/null ) &
watchdog=$!
wait $pid
code=$?
kill -- -$watchdog 2>/dev/null
echo $code > {q(str(rd / 'exit.tmp'))} && mv {q(str(rd / 'exit.tmp'))} {q(str(rd / 'exit'))}
"""


def start(shortcut_id: str, options, device, request=None, device_id: str = "") -> dict:
    shortcut = load(shortcut_id)
    chosen = validate(shortcut, options)
    device = device if isinstance(device, dict) else {}
    device = {"kind": str(device.get("kind") or "")[:16], "name": str(device.get("name") or "")[:80]}
    if not shortcut["concurrent"]:
        busy = next((r for r in runs(shortcut_id, 50) if r["state"] == "running"), None)
        if busy:
            raise RunBusy(busy)
    run_id = time.strftime("r-%Y%m%d-%H%M%S-") + secrets.token_hex(2)
    rd = runs_dir() / run_id
    rd.mkdir(parents=True)
    rec = {"id": run_id, "shortcut": shortcut_id, "name": shortcut["name"],
           "symbol": shortcut["symbol"], "options": chosen, "device": device,
           "state": "running", "exit_code": None, "started_at": time.time(),
           "ended_at": None, "summary": "", "url": "", "announced": False,
           "timeout": shortcut["timeout"], "script": shortcut["script"],
           "folder": shortcut["folder"]}
    env = {f"SHORTCUT_OPT_{k.upper()}": ("1" if v else "0") if isinstance(v, bool) else v
           for k, v in chosen.items()}
    env.update(SHORTCUT_ID=shortcut_id, SHORTCUT_RUN_ID=run_id,
               SHORTCUT_OPTIONS=json.dumps(chosen),
               SHORTCUT_DEVICE_KIND=device["kind"], SHORTCUT_DEVICE_NAME=device["name"],
               SHORTCUT_RESULT=str(rd / "result.json"))
    (rd / "run.sh").write_text(_wrapper(rec, rd, env))
    _write(rd / "run.json", rec)
    r = subprocess.run(managed._t("new-session", "-d", "-s", f"{SESSION_PREFIX}{run_id}",
                                  "-c", shortcut["folder"], "/bin/bash", str(rd / "run.sh")),
                       capture_output=True, text=True, env=_script_env())
    if r.returncode != 0:
        rec.update(state="failed", exit_code=-1, ended_at=time.time(), announced=True,
                   summary=f"Could not start: {(r.stderr or '').strip()[:200]}")
        _write(rd / "run.json", rec)
    _record("shortcut.run", shortcut_id, shortcut["name"], request, device_id,
            run=run_id, options=chosen)
    _prune()
    _ensure_watch()
    return _public_run(rec)


class RunBusy(Exception):
    def __init__(self, run: dict):
        super().__init__(run["id"])
        self.run = run


def cancel(run_id: str, request=None, device_id: str = "") -> dict:
    rd = _rundir(run_id)
    rec = get_run(run_id)
    if rec["state"] != "running":
        return _public_run(rec)
    (rd / "cancelled").touch()
    try:
        pid = int((rd / "pid").read_text().strip())
        os.killpg(pid, 15)
    except (OSError, ValueError):
        subprocess.run(managed._t("kill-session", "-t", f"={SESSION_PREFIX}{run_id}"),
                       capture_output=True)
    _record("shortcut.cancel", rec["shortcut"], rec["name"], request, device_id, run=run_id)
    return _public_run(rec)


def output(run_id: str, offset: int) -> tuple[str, int]:
    log = _rundir(run_id) / "output.log"
    try:
        with log.open("rb") as f:
            f.seek(max(0, offset))
            chunk = f.read(OUTPUT_CHUNK)
    except FileNotFoundError:
        return "", max(0, offset)
    # Hold back a trailing partial UTF-8 sequence for the next poll.
    cut, i = len(chunk), len(chunk) - 1
    while i >= 0 and len(chunk) - i <= 3 and (chunk[i] & 0xC0) == 0x80:
        i -= 1
    if i >= 0 and chunk[i] >= 0xC0:
        need = 2 if chunk[i] < 0xE0 else 3 if chunk[i] < 0xF0 else 4
        if len(chunk) - i < need:
            cut = i
    text = chunk[:cut].decode("utf-8", errors="replace")
    return _ANSI.sub("", text), max(0, offset) + cut


def _prune() -> None:
    root = runs_dir()
    dirs = sorted((p for p in root.iterdir() if p.is_dir()), reverse=True)
    for rd in dirs[KEEP_RUNS:]:
        if _read(rd / "run.json").get("state") == "running":
            continue
        for f in rd.iterdir():
            f.unlink(missing_ok=True)
        rd.rmdir()


# -- the end of a run -------------------------------------------------------

def _announce(rec: dict) -> None:
    if rec.get("announced"):
        return
    rec["announced"] = True
    _write(runs_dir() / rec["id"] / "run.json", rec)
    body = rec.get("summary") or {
        "succeeded": "Succeeded", "cancelled": "Cancelled", "lost": "Lost",
    }.get(rec["state"], f"Failed (exit {rec.get('exit_code')})")
    try:
        from . import notify
        notify.broadcast(title=rec["name"], body=body,
                         extra={"shortcut_run": rec["id"]},
                         collapse_id=f"shortcut-{rec['id']}")
    except Exception as e:  # noqa: BLE001 — a failed push never fails the run
        print(f"run shortcuts: push for {rec['id']} failed ({type(e).__name__}: {e})",
              flush=True)


def _watch_loop() -> None:
    global _watching
    while True:
        time.sleep(3)
        try:
            live = [r for r in runs("", KEEP_RUNS) if r["state"] == "running"]
        except Exception:  # noqa: BLE001
            live = [True]
        if not live:
            with _watch_lock:
                _watching = False
            return


def _ensure_watch() -> None:
    """Settling is lazy (every read settles), so the watcher exists only for
    the push: a run nobody is looking at still has to announce its end."""
    global _watching
    with _watch_lock:
        if _watching:
            return
        _watching = True
    threading.Thread(target=_watch_loop, name="run-shortcuts-watch", daemon=True).start()


def resume_watch() -> None:
    """Called at host start: runs that outlived the last host still announce."""
    try:
        if any(r["state"] == "running" for r in runs("", KEEP_RUNS)):
            _ensure_watch()
    except Exception as e:  # noqa: BLE001
        print(f"run shortcuts: resume skipped ({type(e).__name__}: {e})", flush=True)


def _record(action: str, shortcut_id: str, name: str, request, device_id: str, **detail) -> None:
    try:
        actor = audit.from_request(request, device_id) if request is not None else {}
        actor["detail"] = {**actor.get("detail", {}), **detail}
        from .store import get_store
        with audit.acting(actor):
            get_store().record_access(action, "shortcut", shortcut_id, name)
    except Exception:  # noqa: BLE001 — the record never blocks the run
        pass


# -- HTTP -------------------------------------------------------------------

def _device_id(request: Request) -> str:
    return getattr(request.state, "authorized_device", "") or ""


@router.get("/run-shortcuts")
def list_route():
    root = hostenv.shortcuts_dir()
    if not root.is_dir():
        return {"available": False, "reason": f"No shortcuts folder at {root}",
                "dir": str(root), "shortcuts": []}
    resume_watch()
    return {"available": True, "dir": str(root), "shortcuts": catalog()}


@router.post("/run-shortcuts/{shortcut_id}/run")
async def run_route(shortcut_id: str, request: Request):
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        body = {}
    body = body if isinstance(body, dict) else {}
    try:
        from fastapi.concurrency import run_in_threadpool
        run = await run_in_threadpool(start, shortcut_id, body.get("options"),
                                      body.get("device"), request, _device_id(request))
    except KeyError:
        raise HTTPException(404, f"No shortcut {shortcut_id}")
    except ShortcutError as e:
        raise HTTPException(400, str(e))
    except RunBusy as e:
        return JSONResponse({"detail": f"{e.run['name']} is already running", "run": e.run},
                            status_code=409)
    return {"run": run}


@router.get("/run-shortcuts/runs")
def runs_route(shortcut: str = "", limit: int = 20):
    return {"runs": runs(shortcut, max(1, min(limit, KEEP_RUNS)))}


@router.get("/run-shortcuts/runs/{run_id}")
def run_detail_route(run_id: str, offset: int = 0):
    try:
        rec = get_run(run_id)
        text, new_offset = output(run_id, offset)
    except KeyError:
        raise HTTPException(404, f"No run {run_id}")
    return {"run": _public_run(rec), "output": text, "offset": new_offset}


@router.post("/run-shortcuts/runs/{run_id}/cancel")
def cancel_route(run_id: str, request: Request):
    try:
        return {"run": cancel(run_id, request, _device_id(request))}
    except KeyError:
        raise HTTPException(404, f"No run {run_id}")
