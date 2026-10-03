"""run_shortcuts: the catalog, option validation, a run's whole life in a real
tmux pane, cancel, the push at the end, and the routes.

Runs against a real tmux server on a throwaway socket, never the live
`jremote` one — same convention as `test_hub_shell.py`.
"""

import json
import os
import shutil
import subprocess
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from jstack_host import auth, managed, notify, run_shortcuts
from jstack_host.server import create_app

pytestmark = pytest.mark.skipif(not shutil.which(managed._TMUX),
                                reason="tmux not installed")

app = create_app()


@pytest.fixture
def sock(monkeypatch):
    monkeypatch.setattr(managed, "_SOCK", f"jr-rsc-{os.getpid()}")
    subprocess.run(managed._t("kill-server"), capture_output=True)
    yield
    subprocess.run(managed._t("kill-server"), capture_output=True)


@pytest.fixture
def pushes(monkeypatch):
    sent = []
    monkeypatch.setattr(notify, "broadcast", lambda **kw: sent.append(kw) or True)
    return sent


@pytest.fixture
def shelf(monkeypatch, tmp_path, sock, pushes):
    root = tmp_path / "Shortcuts"
    root.mkdir()
    monkeypatch.setenv("JSTACK_SHORTCUTS_DIR", str(root))
    monkeypatch.setattr(run_shortcuts, "runs_dir", lambda: tmp_path / "runs")
    return root


def make(root: Path, sid: str, manifest: dict, script: str = "#!/bin/bash\necho hi\n",
         files: dict | None = None) -> Path:
    folder = root / sid
    folder.mkdir()
    manifest = {"name": sid.title(), "run": "run.sh", **manifest}
    (folder / "shortcut.json").write_text(json.dumps(manifest))
    for name, body in {"run.sh": script, **(files or {})}.items():
        (folder / name).write_text(body)
        (folder / name).chmod(0o755)
    return folder


def wait_done(run_id: str, timeout: float = 15) -> dict:
    end = time.time() + timeout
    while time.time() < end:
        rec = run_shortcuts.get_run(run_id)
        if rec["state"] != "running":
            return rec
        time.sleep(0.2)
    raise AssertionError(f"{run_id} still running after {timeout}s")


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(auth, "_expected_token", lambda: "test-token")
    c = TestClient(app)
    c.headers.update({"Authorization": "Bearer test-token"})
    return c


# ── the catalog ─────────────────────────────────────────────────────────────

def test_options_resolve_static_and_sourced_values(shelf):
    make(shelf, "build", {"symbol": "hammer", "options": [
        {"id": "app", "type": "choice", "source": "apps.sh", "default": "b"},
        {"id": "away", "type": "choice", "values": ["firebase", {"value": "tf", "label": "TestFlight"}]},
        {"id": "dev", "type": "toggle"},
        {"id": "notes", "type": "text", "placeholder": "why"},
    ]}, files={"apps.sh": "#!/bin/bash\necho '[{\"value\":\"a\",\"label\":\"Alpha\"},\"b\"]'\n"})
    [s] = run_shortcuts.catalog()
    assert s["error"] == "" and s["symbol"] == "hammer"
    app_opt, away, dev, notes = s["options"]
    assert app_opt["values"] == [{"value": "a", "label": "Alpha"}, {"value": "b", "label": "b"}]
    assert app_opt["default"] == "b"
    assert away["default"] == "firebase" and away["values"][1]["label"] == "TestFlight"
    assert dev["default"] is False and notes["placeholder"] == "why"


def test_a_source_may_print_tab_separated_lines(shelf):
    make(shelf, "s", {"options": [{"id": "x", "type": "choice", "source": "v.sh"}]},
         files={"v.sh": "#!/bin/bash\nprintf 'one\\tOne\\ntwo\\n'\n"})
    [s] = run_shortcuts.catalog()
    assert s["options"][0]["values"] == [{"value": "one", "label": "One"},
                                         {"value": "two", "label": "two"}]


def test_a_default_outside_the_choices_falls_to_the_first(shelf):
    make(shelf, "s", {"options": [{"id": "x", "type": "choice", "values": ["a", "b"],
                                   "default": "zzz"}]})
    assert run_shortcuts.catalog()[0]["options"][0]["default"] == "a"


@pytest.mark.parametrize("manifest,files,fragment", [
    ({"options": [{"id": "x", "type": "slider"}]}, {}, "type must be"),
    ({"options": [{"id": "Bad-Id", "type": "toggle"}]}, {}, "option id"),
    ({"options": [{"id": "x", "type": "toggle"}, {"id": "x", "type": "text"}]}, {}, "unique"),
    ({"run": "../escape.sh"}, {}, "inside the shortcut"),
    ({"run": "missing.sh"}, {}, "does not exist"),
    ({"options": [{"id": "x", "type": "choice", "source": "bad.sh"}]},
     {"bad.sh": "#!/bin/bash\necho nope >&2\nexit 3\n"}, "exited 3"),
])
def test_a_broken_shortcut_is_listed_with_its_error(shelf, manifest, files, fragment):
    make(shelf, "broken", manifest, files=files)
    [s] = run_shortcuts.catalog()
    assert fragment in s["error"]


def test_a_script_others_can_write_is_refused(shelf):
    folder = make(shelf, "s", {})
    (folder / "run.sh").chmod(0o777)
    [s] = run_shortcuts.catalog()
    assert "writable by other users" in s["error"]
    with pytest.raises(run_shortcuts.ShortcutError):
        run_shortcuts.start("s", {}, {})


def test_folders_that_are_not_shortcuts_are_skipped(shelf):
    (shelf / "notes").mkdir()
    (shelf / "Bad Name").mkdir()
    (shelf / "Bad Name" / "shortcut.json").write_text("{}")
    assert run_shortcuts.catalog() == []


# ── validation ──────────────────────────────────────────────────────────────

@pytest.fixture
def shaped(shelf):
    make(shelf, "s", {"options": [
        {"id": "app", "type": "choice", "values": ["a", "b"]},
        {"id": "dev", "type": "toggle", "default": True},
        {"id": "notes", "type": "text"},
    ]})
    return run_shortcuts.load("s")


def test_missing_options_take_their_defaults(shaped):
    assert run_shortcuts.validate(shaped, {}) == {"app": "a", "dev": True, "notes": ""}


@pytest.mark.parametrize("given,fragment", [
    ({"app": "c"}, "not one of the choices"),
    ({"dev": "yes"}, "on or off"),
    ({"notes": 3}, "must be text"),
    ({"notes": "x" * 5000}, "too long"),
    ({"other": "1"}, "unknown option other"),
])
def test_bad_values_are_refused(shaped, given, fragment):
    with pytest.raises(run_shortcuts.ShortcutError, match=fragment):
        run_shortcuts.validate(shaped, given)


# ── a run ───────────────────────────────────────────────────────────────────

def test_a_run_sees_its_options_and_device_and_reports_a_result(shelf, pushes):
    make(shelf, "s", {"options": [{"id": "app", "type": "choice", "values": ["wordy"]},
                                  {"id": "dev", "type": "toggle"}]},
         script='#!/bin/bash\necho "app=$SHORTCUT_OPT_APP dev=$SHORTCUT_OPT_DEV '
                'kind=$SHORTCUT_DEVICE_KIND"\n'
                'printf \'{"summary":"Installed","url":"https://x"}\' > "$SHORTCUT_RESULT"\n')
    run = run_shortcuts.start("s", {"app": "wordy"}, {"kind": "iphone", "name": "Phone"})
    assert run["state"] == "running"
    done = wait_done(run["id"])
    assert done["state"] == "succeeded" and done["exit_code"] == 0
    assert done["summary"] == "Installed" and done["url"] == "https://x"
    text, offset = run_shortcuts.output(run["id"], 0)
    assert text.strip() == "app=wordy dev=0 kind=iphone"
    assert run_shortcuts.output(run["id"], offset) == ("", offset)
    assert [p["title"] for p in pushes] == ["S"]
    assert pushes[0]["body"] == "Installed"
    assert pushes[0]["extra"] == {"shortcut_run": run["id"]}


def test_a_failing_run_reads_failed_and_pushes_once(shelf, pushes):
    make(shelf, "s", {}, script="#!/bin/bash\necho boom\nexit 4\n")
    run = run_shortcuts.start("s", {}, {})
    done = wait_done(run["id"])
    assert done["state"] == "failed" and done["exit_code"] == 4
    run_shortcuts.get_run(run["id"])
    run_shortcuts.runs("s")
    assert len(pushes) == 1 and pushes[0]["body"] == "Failed (exit 4)"


def test_output_is_ansi_stripped(shelf):
    make(shelf, "s", {}, script="#!/bin/bash\nprintf '\\033[31mred\\033[0m\\n'\n")
    run = run_shortcuts.start("s", {}, {})
    wait_done(run["id"])
    assert run_shortcuts.output(run["id"], 0)[0] == "red\n"


def test_output_never_splits_a_character(shelf, tmp_path):
    make(shelf, "s", {}, script="#!/bin/bash\nprintf 'ab\\xc3'\n")
    run = run_shortcuts.start("s", {}, {})
    wait_done(run["id"])
    assert run_shortcuts.output(run["id"], 0) == ("ab", 2)


def test_one_run_at_a_time_unless_concurrent(shelf):
    make(shelf, "s", {}, script="#!/bin/bash\nsleep 30\n")
    first = run_shortcuts.start("s", {}, {})
    with pytest.raises(run_shortcuts.RunBusy) as busy:
        run_shortcuts.start("s", {}, {})
    assert busy.value.run["id"] == first["id"]
    run_shortcuts.cancel(first["id"])
    make(shelf, "c", {"concurrent": True}, script="#!/bin/bash\nsleep 30\n")
    a, b = run_shortcuts.start("c", {}, {}), run_shortcuts.start("c", {}, {})
    assert a["id"] != b["id"]
    for r in (a, b):
        run_shortcuts.cancel(r["id"])


def test_cancel_takes_the_children_down(shelf, tmp_path):
    marker = tmp_path / "child.pid"
    make(shelf, "s", {}, script=f"#!/bin/bash\nsleep 300 &\necho $! > {marker}\nwait\n")
    run = run_shortcuts.start("s", {}, {})
    for _ in range(50):
        if marker.exists() and marker.read_text().strip():
            break
        time.sleep(0.1)
    child = int(marker.read_text())
    run_shortcuts.cancel(run["id"])
    assert wait_done(run["id"])["state"] == "cancelled"
    time.sleep(0.3)
    with pytest.raises(ProcessLookupError):
        os.kill(child, 0)


def test_a_run_past_its_timeout_is_stopped(shelf):
    make(shelf, "s", {"timeout": 1}, script="#!/bin/bash\nsleep 60\n")
    run = run_shortcuts.start("s", {}, {})
    done = wait_done(run["id"], timeout=20)
    assert done["state"] == "failed" and done["summary"] == "Stopped after 1s"


def test_a_run_whose_pane_vanished_reads_lost(shelf, monkeypatch):
    make(shelf, "s", {}, script="#!/bin/bash\nsleep 60\n")
    run = run_shortcuts.start("s", {}, {})
    pid = int((run_shortcuts.runs_dir() / run["id"] / "pid").read_text())
    subprocess.run(managed._t("kill-server"), capture_output=True)
    os.killpg(pid, 9)
    rec = json.loads((run_shortcuts.runs_dir() / run["id"] / "run.json").read_text())
    rec["started_at"] -= 10
    (run_shortcuts.runs_dir() / run["id"] / "run.json").write_text(json.dumps(rec))
    assert run_shortcuts.get_run(run["id"])["state"] == "lost"


def test_run_ids_cannot_reach_outside_the_runs_dir(shelf):
    for bad in ("../x", "r-1", "r-20261002-120000-zzzz/.."):
        with pytest.raises(KeyError):
            run_shortcuts.get_run(bad)


# ── the routes ──────────────────────────────────────────────────────────────

def test_routes_list_run_poll_and_cancel(shelf, client):
    make(shelf, "s", {"symbol": "bolt", "options": [{"id": "app", "type": "choice",
                                                     "values": ["a"]}]},
         script="#!/bin/bash\necho out\nsleep 30\n")
    listing = client.get("/api/jremote/v1/run-shortcuts").json()
    assert listing["available"] and listing["shortcuts"][0]["id"] == "s"
    assert listing["shortcuts"][0]["last_run"] is None

    assert client.post("/api/jremote/v1/run-shortcuts/s/run",
                       json={"options": {"app": "nope"}}).status_code == 400
    assert client.post("/api/jremote/v1/run-shortcuts/zz/run", json={}).status_code == 404

    run = client.post("/api/jremote/v1/run-shortcuts/s/run",
                      json={"options": {"app": "a"}, "device": {"kind": "ipad"}}).json()["run"]
    busy = client.post("/api/jremote/v1/run-shortcuts/s/run", json={})
    assert busy.status_code == 409 and busy.json()["run"]["id"] == run["id"]

    for _ in range(50):
        detail = client.get(f"/api/jremote/v1/run-shortcuts/runs/{run['id']}").json()
        if detail["output"]:
            break
        time.sleep(0.1)
    assert detail["output"] == "out\n" and detail["run"]["device"]["kind"] == "ipad"

    client.post(f"/api/jremote/v1/run-shortcuts/runs/{run['id']}/cancel")
    wait_done(run["id"])
    history = client.get("/api/jremote/v1/run-shortcuts/runs?shortcut=s").json()["runs"]
    assert [r["state"] for r in history] == ["cancelled"]
    assert "script" not in history[0] and "folder" not in history[0]


def test_no_folder_reads_unavailable_not_error(client, monkeypatch, tmp_path):
    monkeypatch.setenv("JSTACK_SHORTCUTS_DIR", str(tmp_path / "absent"))
    body = client.get("/api/jremote/v1/run-shortcuts").json()
    assert body["available"] is False and body["shortcuts"] == []


def test_routes_need_the_token():
    assert TestClient(app).get("/api/jremote/v1/run-shortcuts").status_code == 401


def test_the_push_payload_carries_the_run(monkeypatch):
    from jstack_host import apns
    captured = {}

    class FakeResp:
        status_code = 200
        text = ""

    class FakeClient:
        def __init__(self, **kw): pass
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def post(self, url, headers, content):
            captured["payload"] = json.loads(content)
            return FakeResp()

    monkeypatch.setattr(apns, "_config", lambda: {"bundle_id": "b", "sandbox": True})
    monkeypatch.setattr(apns, "is_configured", lambda: True)
    monkeypatch.setattr(apns, "_provider_token", lambda cfg: "t")
    monkeypatch.setattr(apns.httpx, "Client", FakeClient)
    ok, _ = apns.send("tok", title="T", body="B", extra={"shortcut_run": "r-1",
                                                          "session_id": "spoof"})
    assert ok
    assert captured["payload"]["shortcut_run"] == "r-1"
    assert captured["payload"]["session_id"] == ""
