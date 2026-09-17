"""Opening a session ON a subject — jRemote's side of the timeline's tags.

A seat answers "who", a date answers "when". The tag is the third axis, and a
pinned tag is a cockpit: the session comes up reading every agent's work on
that subject instead of its own seat's history, and files its own entries
under it. The host's part is three seams, and each one has a way to lie:

  * `timeline.py` — read-only access to the minted vocabulary. It must never
    write, must resolve the NEWEST binary by parsed version, and must be able
    to say "this host cannot see tags" distinctly from "no tags exist".
  * `POST /sessions/open-new` — refuses a tag nobody minted rather than
    minting it or, far worse, dropping it and opening a session that looks
    exactly like the pin worked.
  * `open_managed` / `record_open` — the pin reaches the agent as an exported
    env var, and it has to SURVIVE: every reopen path re-registers with
    nothing but a sid, and a pin lost there is a row that quietly changes what
    it knows.
"""

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from jstack_host.server import create_app

app = create_app()
from jstack_host import auth, managed, timeline


# ── the vocabulary, read-only ───────────────────────────────────────────────

def test_normalize_matches_the_writers(monkeypatch):
    """A pin stored here and a tag matched by `log_event` must be one string —
    the app sends what a human typed, hash and case and all."""
    assert timeline.normalize("  #jRemote  ") == "jremote"
    assert timeline.normalize("") == ""
    assert timeline.normalize(None) == ""


def test_binary_prefers_dev_checkout(monkeypatch, tmp_path):
    home = tmp_path
    dev = home / "jStack" / "plugins" / "jstack" / "bin" / "log_event"
    dev.parent.mkdir(parents=True)
    dev.write_text("#!/bin/sh\n")
    cache = home / ".claude/plugins/cache/jStack/jstack/9.9.9/bin/log_event"
    cache.parent.mkdir(parents=True)
    cache.write_text("#!/bin/sh\n")
    monkeypatch.setattr(timeline.Path, "home", staticmethod(lambda: home))
    # On the machine that maintains the plugin, the checkout is the source of
    # truth — answering from an installed cache there would offer a vocabulary
    # the writers have already moved past.
    assert timeline.log_event_bin() == dev


def test_cache_version_sort_parses_not_sorts_lexically(monkeypatch, tmp_path):
    home = tmp_path
    for v in ("0.8.0", "0.29.0", "0.10.0"):
        p = home / f".claude/plugins/cache/jStack/jstack/{v}/bin/log_event"
        p.parent.mkdir(parents=True)
        p.write_text("#!/bin/sh\n")
    monkeypatch.setattr(timeline.Path, "home", staticmethod(lambda: home))
    got = timeline.log_event_bin()
    # Lexicographic order ranks "0.8.0" top and would pin the host to a version
    # three releases stale, silently, forever.
    assert got.parent.parent.name == "0.29.0"


def test_absent_jstack_is_unavailable_not_empty(monkeypatch, tmp_path):
    monkeypatch.setattr(timeline.Path, "home", staticmethod(lambda: tmp_path))
    assert timeline.available() is False
    assert timeline.tags() == []
    assert timeline.known("jremote") is False


def _fake_bin(tmp_path, stdout: str, rc: int = 0):
    """A stand-in `log_event` that prints `stdout` for any argv."""
    p = tmp_path / "log_event"
    p.write_text(f"#!/bin/sh\ncat <<'EOF'\n{stdout}\nEOF\nexit {rc}\n")
    p.chmod(0o755)
    return p


def test_tags_reads_the_json_roster(monkeypatch, tmp_path):
    roster = json.dumps([{"name": "jremote", "description": "the app",
                          "sessions": 4},
                         {"name": "infra", "description": "plumbing",
                          "sessions": 1},
                         {"description": "nameless"}])
    monkeypatch.setattr(timeline, "log_event_bin",
                        lambda: _fake_bin(tmp_path, roster))
    got = timeline.tags()
    # Order is the binary's — busiest first — and a row with no name is not a
    # tag anything could ever be pinned to.
    assert [t["name"] for t in got] == ["jremote", "infra"]
    assert timeline.known("#JREMOTE") is True
    assert timeline.known("jremot") is False


@pytest.mark.parametrize("stdout,rc", [("not json", 0), ("[]", 3)])
def test_a_broken_binary_costs_the_tags_never_the_board(monkeypatch, tmp_path,
                                                        stdout, rc):
    monkeypatch.setattr(timeline, "log_event_bin",
                        lambda: _fake_bin(tmp_path, stdout, rc))
    assert timeline.tags() == []


def test_timeline_module_never_writes():
    """The store reserves writes for `bin/log_event` so there is exactly one
    writer. A UI that could mint would grow the vocabulary by typo, and a small
    vocabulary meaning the same thing to every writer is the entire value."""
    src = (timeline.__file__ and open(timeline.__file__).read()) or ""
    for forbidden in ("tag new", "sqlite3", "INSERT", "log_event\", \"tag\", \"set"):
        assert forbidden not in src, f"timeline.py must not write: {forbidden!r}"


# ── the routes ──────────────────────────────────────────────────────────────

@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(auth, "_expected_token", lambda: "test-token")
    c = TestClient(app)
    c.headers.update({"Authorization": "Bearer test-token"})
    return c


@pytest.fixture
def vocabulary(monkeypatch, tmp_path):
    """A host that can see tags, and knows exactly one."""
    roster = json.dumps([{"name": "jremote", "description": "the app",
                          "sessions": 4}])
    monkeypatch.setattr(timeline, "log_event_bin",
                        lambda: _fake_bin(tmp_path, roster))


@pytest.fixture
def no_timeline(monkeypatch):
    monkeypatch.setattr(timeline, "log_event_bin", lambda: None)


def test_tags_route_serves_the_roster(client, vocabulary):
    r = client.get("/api/jremote/v1/tags")
    assert r.status_code == 200
    assert [t["name"] for t in r.json()["tags"]] == ["jremote"]


def test_tags_route_says_absent_not_empty(client, no_timeline):
    """A picker drawing zero rows must be able to say why. `available: false`
    is a different answer from an empty vocabulary, and the app renders them
    differently — one offers a mint, the other explains the host."""
    r = client.get("/api/jremote/v1/tags")
    assert r.status_code == 200
    body = r.json()
    assert body["available"] is False and body["tags"] == []
    assert "not available" in body["reason"]


def test_host_capability_map_covers_tags(client, vocabulary, no_timeline):
    # no_timeline wins (applied last), so this asserts the probe answers from
    # the same call the route makes rather than from a static feature list.
    assert client.get("/api/jremote/v1/host").json()["features"]["tags"] is False


# ── filing a tag from the app ───────────────────────────────────────────────

_SID = "3eee625d-6976-482a-8709-b8e7ea4d6526"


def _recording_bin(tmp_path, rc: int = 0, stderr: str = ""):
    """A stand-in `log_event` that writes its argv to `argv.txt` and exits `rc`.

    The argv is the whole point: this route's contract is not "it returned 200",
    it is "the timeline's one writer was handed exactly these words". A test
    that only read the status code would pass just as happily on a route that
    shelled out to nothing.
    """
    log = tmp_path / "argv.txt"
    p = tmp_path / "log_event"
    p.write_text(f'#!/bin/sh\nprintf "%s\\n" "$@" > {log}\n'
                 f'>&2 printf "%s" "{stderr}"\nexit {rc}\n')
    p.chmod(0o755)
    return p, log


@pytest.fixture
def writer(monkeypatch, tmp_path):
    from jstack_host import board_watch
    monkeypatch.setattr(board_watch, "poke", lambda: None)
    binary, log = _recording_bin(tmp_path)
    monkeypatch.setattr(timeline, "log_event_bin", lambda: binary)
    return log


def _post(client, **body):
    return client.post(f"/api/jremote/v1/sessions/{_SID}/tags", json=body)


@pytest.mark.parametrize("verb", ["set", "unset"])
def test_filing_shells_out_to_the_one_writer(client, writer, verb):
    r = _post(client, verb=verb, name="jremote")
    assert r.status_code == 200 and r.json() == {"ok": True}
    assert writer.read_text().split() == ["tag", verb, "jremote",
                                          "--session", _SID]


def test_the_name_is_normalized_before_it_is_written(client, writer):
    """The app sends what a thumb typed. `log_event` matches on the normalized
    string, so a route that passed `#JRemote` through would file a session under
    a name every reader looks for under a different one."""
    assert _post(client, verb="set", name="  #JRemote  ").status_code == 200
    assert writer.read_text().split()[2] == "jremote"


def test_the_verb_is_case_and_space_tolerant(client, writer):
    """Normalized like the name is, and for the same reason — the gate below
    tests what the verb MEANS, so it must not also be a spelling test."""
    assert _post(client, verb=" SET ", name="jremote").status_code == 200
    assert writer.read_text().split()[1] == "set"


@pytest.mark.parametrize("verb", ["new", "list", "show", "delete", ""])
def test_only_set_and_unset_may_ever_reach_the_binary(client, writer, verb):
    """The gate that matters. `tag new` through this route would let a thumb
    mint a tag with no description — and the description is what the next
    session matches against, so the vocabulary would fork into synonyms and the
    axis would stop answering. Restricted here rather than left to the binary,
    which would happily accept `new`."""
    r = _post(client, verb=verb, name="brand-new-subject")
    assert r.status_code == 400
    assert not writer.exists(), "the binary must not have been reached at all"


def test_an_empty_name_never_reaches_the_binary(client, writer):
    r = _post(client, verb="set", name="  #  ")
    assert r.status_code == 400
    assert not writer.exists()


def test_an_unminted_name_surfaces_the_binarys_own_refusal(client, monkeypatch,
                                                           tmp_path):
    """`log_event tag set` already refuses a name nobody minted. The route
    inherits that gate instead of duplicating it — and must hand back what the
    writer actually said, because "no tag 'deploment'" is actionable where a
    bare 400 is not."""
    from jstack_host import board_watch
    monkeypatch.setattr(board_watch, "poke", lambda: None)
    binary, _ = _recording_bin(tmp_path, rc=1, stderr="no tag 'deploment'")
    monkeypatch.setattr(timeline, "log_event_bin", lambda: binary)
    r = _post(client, verb="set", name="deploment")
    assert r.status_code == 400
    assert "no tag 'deploment'" in r.json()["detail"]


def test_filing_on_a_host_with_no_timeline_is_503_not_a_quiet_ok(client,
                                                                no_timeline):
    """The failure the app must be able to draw. A 200 here would leave a chip
    on a card claiming a filing that no store anywhere records."""
    r = _post(client, verb="set", name="jremote")
    assert r.status_code == 503
    assert "no timeline" in r.json()["detail"]


def test_a_hostile_session_id_is_refused_before_the_shell(client, writer):
    r = client.post("/api/jremote/v1/sessions/..%2Fetc%2Fpasswd/tags",
                    json={"verb": "set", "name": "jremote"})
    assert r.status_code in (400, 404)
    assert not writer.exists()


def test_a_successful_filing_repaints_the_board(client, monkeypatch, tmp_path):
    """The card carries the tag, so the board is what has changed. Without the
    poke the chip appears on the next tick instead of on the tap."""
    from jstack_host import board_watch
    poked = []
    monkeypatch.setattr(board_watch, "poke", lambda: poked.append(True))
    binary, _ = _recording_bin(tmp_path)
    monkeypatch.setattr(timeline, "log_event_bin", lambda: binary)
    assert _post(client, verb="set", name="jremote").status_code == 200
    assert poked == [True]


# ── the pin's gate on spawn ─────────────────────────────────────────────────

@pytest.fixture
def spawn_env(monkeypatch, tmp_path):
    # `hostenv`, not the tree behind it: the package asks the seam, and a
    # fixture that reaches past it to the machine's own registry only works on
    # a machine that has one.
    from jstack_host import board_watch, hostenv
    monkeypatch.setattr(hostenv, "workspace", lambda a: tmp_path)
    monkeypatch.setattr(hostenv, "split_id", lambda a: (a, None))
    calls = {}

    def fake_open(sid, cwd, resume=True, displace=None, nudge=None,
                  extra="", prelude="", window=False, engine="claude",
                  model="", tag=""):
        calls.update(sid=sid, tag=tag)

    def fake_record(sid, base, name="", engine="claude", model="", tag=""):
        calls.update(recorded_tag=tag)

    monkeypatch.setattr(managed, "open_managed", fake_open)
    monkeypatch.setattr(managed, "record_open", fake_record)
    monkeypatch.setattr(board_watch, "poke", lambda: None)
    return calls


def test_pin_reaches_both_the_spawn_and_the_registry(client, vocabulary,
                                                     spawn_env):
    r = client.post("/api/jremote/v1/sessions/open-new",
                    json={"agent_id": "testa", "tag": "#JRemote"})
    assert r.status_code == 200
    # Normalized once, at the door: the pane's env, the registry and the
    # response all carry the same string the timeline will match on.
    assert r.json()["tag"] == "jremote"
    assert spawn_env["tag"] == "jremote"
    assert spawn_env["recorded_tag"] == "jremote"


def test_unminted_tag_is_refused_not_created(client, vocabulary, spawn_env):
    r = client.post("/api/jremote/v1/sessions/open-new",
                    json={"agent_id": "testa", "tag": "jremot"})
    assert r.status_code == 400
    assert "log_event tag new" in r.json()["detail"]
    # And nothing was spawned — a refused pin must not leave a session behind
    # sitting on the wrong history.
    assert spawn_env == {}


def test_pin_on_a_host_with_no_timeline_is_503(client, no_timeline, spawn_env):
    r = client.post("/api/jremote/v1/sessions/open-new",
                    json={"agent_id": "testa", "tag": "jremote"})
    assert r.status_code == 503
    assert spawn_env == {}


def test_no_pin_is_the_ordinary_session(client, no_timeline, spawn_env):
    """The timeline being absent must cost only the pin. An unpinned spawn on
    a host with no jStack is the normal case, not a degraded one."""
    r = client.post("/api/jremote/v1/sessions/open-new",
                    json={"agent_id": "testa"})
    assert r.status_code == 200
    assert spawn_env["tag"] == ""


# ── the subjects a shortcut files under, on top of the one it opens on ──────

def _vocabulary_and_recorder(tmp_path, names, rc: int = 0):
    """One stand-in `log_event` that answers BOTH questions this path asks it:
    `tag list --json` for the vocabulary gate, `tag set` for the filing — and
    records the filing's argv. Two separate fakes cannot cover it, because the
    route validates and then writes through the same binary."""
    log = tmp_path / "argv.txt"
    roster = json.dumps([{"name": n, "description": n, "sessions": 1}
                         for n in names])
    p = tmp_path / "log_event"
    p.write_text(
        "#!/bin/sh\n"
        'if [ "$2" = "list" ]; then\n'
        f"cat <<'EOF'\n{roster}\nEOF\n"
        "exit 0\nfi\n"
        f'printf "%s\\n" "$@" > {log}\n'
        f"exit {rc}\n")
    p.chmod(0o755)
    return p, log


def test_carried_tags_are_filed_and_the_pin_is_not_re_filed(client, spawn_env,
                                                            monkeypatch,
                                                            tmp_path):
    """A shortcut may carry several subjects. One of them is the cockpit —
    that one the hook files itself at the session's first instant — and the
    rest are filing, done here in a single `log_event` call."""
    binary, log = _vocabulary_and_recorder(
        tmp_path, ["jremote", "infra", "deployment"])
    monkeypatch.setattr(timeline, "log_event_bin", lambda: binary)
    r = client.post("/api/jremote/v1/sessions/open-new",
                    json={"agent_id": "testa", "tag": "jremote",
                          "tags": ["#Infra", "jremote", "deployment"]})
    assert r.status_code == 200
    # The pin is dropped out of the filing list — the hook already carries it.
    assert r.json()["tags"] == ["infra", "deployment"]
    args = log.read_text().split()
    assert args[:4] == ["tag", "set", "infra", "deployment"]
    assert args[4] == "--session" and args[5] == spawn_env["sid"]


def test_an_unminted_carried_tag_is_refused_before_the_spawn(client, vocabulary,
                                                             spawn_env):
    """Same gate as the pin, for the same reason: a shortcut naming a subject
    nobody minted would look like it worked and file nowhere."""
    r = client.post("/api/jremote/v1/sessions/open-new",
                    json={"agent_id": "testa", "tags": ["jremot"]})
    assert r.status_code == 400
    assert spawn_env == {}


def test_a_filing_that_fails_does_not_cost_the_session(client, spawn_env,
                                                       monkeypatch, tmp_path):
    """By the time the filing runs the chat is up. A refusal from the writer
    is logged and swallowed — raising here would show the user an error for a
    session that is running fine."""
    binary, _ = _vocabulary_and_recorder(tmp_path, ["infra"], rc=1)
    monkeypatch.setattr(timeline, "log_event_bin", lambda: binary)
    r = client.post("/api/jremote/v1/sessions/open-new",
                    json={"agent_id": "testa", "tags": ["infra"]})
    assert r.status_code == 200
    assert spawn_env["sid"]


@pytest.fixture
def launch_store(tmp_path, monkeypatch):
    """The spawn's provenance write, against a scratch store — the route
    reaches the process-wide one, and a test must not put rows in this Mac's
    live index."""
    from jstack_host import store as store_mod
    s = store_mod.SessionStore(db_path=tmp_path / "launch.sqlite")
    monkeypatch.setattr(store_mod, "get_store", lambda: s)
    return s


def test_the_launching_shortcut_is_recorded(client, vocabulary, spawn_env,
                                            launch_store):
    """So the card can show its own sittings later. Provenance only — nothing
    about how the session runs is read back out of it."""
    r = client.post("/api/jremote/v1/sessions/open-new",
                    json={"agent_id": "testa", "shortcut_id": "sc-1"})
    assert r.status_code == 200
    assert launch_store.launched_by("sc-1") == {r.json()["session_id"]}


def test_a_spawn_with_no_shortcut_records_nothing(client, vocabulary, spawn_env,
                                                  launch_store):
    """A desk-side claude, a takeover and a share all open without one, and
    that is the resting state — not a blank row."""
    r = client.post("/api/jremote/v1/sessions/open-new", json={"agent_id": "testa"})
    assert r.status_code == 200
    assert launch_store.launch_shortcuts() == {}


# ── the pin survives, or it is not a pin ────────────────────────────────────

@pytest.fixture
def reg(tmp_path, monkeypatch):
    monkeypatch.setattr(managed, "_REG", tmp_path / "jremote_open.json")
    return tmp_path / "jremote_open.json"


def test_registry_records_the_pin(reg):
    managed.record_open("s1", "nova", tag="jremote")
    assert json.loads(reg.read_text())["s1"] == {"agent": "nova",
                                                 "tag": "jremote"}


def test_reopen_keeps_the_pin_the_engine_and_the_model(reg):
    """Every reopen path — board tap, PTY attach, displace, fork — registers
    again with nothing but a sid and an agent, because that is all a reopen
    knows. A plain overwrite drops three facts the spawn is the only witness
    to: a reopened Codex row would relabel itself claude, and a pinned session
    would come back up reading its seat's history instead of its subject's."""
    managed.record_open("s1", "nova", engine="codex", model="gpt-5",
                        tag="jremote")
    managed.record_open("s1", "nova")          # the reopen
    assert json.loads(reg.read_text())["s1"] == {
        "agent": "nova", "engine": "codex", "model": "gpt-5",
        "tag": "jremote"}


def test_an_explicit_pin_still_wins(reg):
    managed.record_open("s1", "nova", tag="jremote")
    managed.record_open("s1", "nova", tag="infra")
    assert json.loads(reg.read_text())["s1"]["tag"] == "infra"


def test_an_unpinned_session_records_no_tag_key(reg):
    """Existing registry entries keep their exact shape — same contract the
    engine field has, so a rollback reads the file unchanged."""
    managed.record_open("s2", "orin")
    assert json.loads(reg.read_text())["s2"] == {"agent": "orin"}


# ── the pin as the agent actually receives it ───────────────────────────────

TEST_SOCK = "jrtest-tags"
SID = "bbbbbbbb-1111-2222-3333-444444444444"

pytestmark_tmux = pytest.mark.skipif(not shutil.which(managed._TMUX),
                                     reason="tmux not installed")


@pytest.fixture
def pane(monkeypatch, reg):
    """A private tmux socket whose pane never actually execs an agent.

    The send-keys line is intercepted and recorded instead of typed, so the
    assertion is on the exact string the pane's shell would run — and no
    `claude` starts, which is what makes this test cheap enough to keep.
    """
    import uuid
    monkeypatch.setitem(globals(), "TEST_SOCK", "jr-tags-" + uuid.uuid4().hex)
    monkeypatch.setattr(managed, "_SOCK", TEST_SOCK)
    monkeypatch.setattr(managed, "_auto_accept_bypass", lambda name: None)
    sent = []
    real_run = managed.subprocess.run

    def spy(cmd, *a, **k):
        if isinstance(cmd, list) and "send-keys" in cmd:
            sent.append(cmd)
            return subprocess.CompletedProcess(cmd, 0, "", "")
        return real_run(cmd, *a, **k)

    monkeypatch.setattr(managed.subprocess, "run", spy)
    yield sent
    real_run([managed._TMUX, "-L", TEST_SOCK, "kill-server"],
             capture_output=True)


def _line(sent) -> str:
    """The shell line the pane actually runs.

    Typed into the pane is only `source <boot>` — nothing that scales with
    caller input may be typed, because a fresh pane's tty is still in canonical
    mode and drops everything past MAX_CANON. The command itself lives in the
    boot file, so that is what these assertions have to read.
    """
    typed = next(c[-1] for c in sent if "-l" in c)
    return Path(typed.split(None, 1)[1]).read_text()


@pytestmark_tmux
def test_pin_is_exported_into_the_pane(pane):
    managed.open_managed(SID, os.path.expanduser("~"), resume=False,
                         tag="jremote")
    line = _line(pane)
    # Env and not a CLI flag: both engines run the same SessionStart hook, and
    # the pin has to survive whatever the shell does before `exec`.
    assert "export JSTACK_TIMELINE_TAG=jremote;" in line
    assert line.index("JSTACK_TIMELINE_TAG") < line.index("exec "), \
        "the pin must be set before the agent replaces the shell"


@pytestmark_tmux
def test_an_unpinned_open_sets_nothing(pane):
    managed.open_managed(SID, os.path.expanduser("~"), resume=False)
    assert "JSTACK_TIMELINE_TAG" not in _line(pane)


@pytestmark_tmux
def test_a_hostile_tag_cannot_escape_the_export(pane):
    """The pin is caller-supplied text typed into a shell. The route refuses
    anything unminted, but quoting is the layer that must hold on its own."""
    managed.open_managed(SID, os.path.expanduser("~"), resume=False,
                         tag="a; rm -rf ~")
    line = _line(pane)
    assert "export JSTACK_TIMELINE_TAG='a; rm -rf ~';" in line


@pytestmark_tmux
def test_reopen_reads_the_pin_back_from_the_registry(pane):
    """No reopen path passes a tag — they all call `open_managed(resume=True)`
    with a sid. Without the read-back the same board row would come up on its
    seat's history, silently changing what it knows."""
    managed.record_open(SID, "nova", tag="jremote")
    managed.open_managed(SID, os.path.expanduser("~"), resume=True)
    assert "export JSTACK_TIMELINE_TAG=jremote;" in _line(pane)


# --- editing the vocabulary itself -------------------------------------------
# Filing a tag and owning the tag are different powers, and they are different
# routes for that reason. These pin the second: what argv reaches the one
# writer, and which refusals the route makes itself instead of delegating.


def _tags_url(name: str = "") -> str:
    return "/api/jremote/v1/tags" + (f"/{name}" if name else "")


def test_minting_shells_out_with_the_description_attached(client, writer):
    """The description is not optional decoration — it is the whole gate on a
    mint, so it must reach the binary as its own flag, not be dropped when the
    route decides the name looked fine on its own."""
    r = client.post(_tags_url(), json={"name": "  #JRemote ",
                                       "description": "the iOS remote app"})
    assert r.status_code == 200, r.text
    assert writer.read_text().split("\n")[:-1] == [
        "tag", "new", "jremote", "--description", "the iOS remote app"]


def test_a_mint_with_no_description_is_the_binarys_refusal_not_a_200(client,
                                                                    monkeypatch,
                                                                    tmp_path):
    """The route deliberately does not pre-check the description: `log_event`
    refuses with a sentence that says what to write instead, and a second copy
    of the rule here is a second thing to keep in agreement with it."""
    from jstack_host import board_watch
    monkeypatch.setattr(board_watch, "poke", lambda: None)
    binary, _ = _recording_bin(tmp_path, rc=2,
                               stderr="'x' needs --description: one line saying "
                                      "what work belongs under it")
    monkeypatch.setattr(timeline, "log_event_bin", lambda: binary)
    r = client.post(_tags_url(), json={"name": "x", "description": ""})
    assert r.status_code == 400
    assert "needs --description" in r.json()["detail"]


def test_an_empty_name_never_reaches_the_binary_on_mint(client, writer):
    r = client.post(_tags_url(), json={"name": "  #  ", "description": "x"})
    assert r.status_code == 400
    assert not writer.exists()


def test_renaming_moves_the_name_and_nothing_else(client, writer):
    r = client.patch(_tags_url("jremot"), json={"name": "jremote"})
    assert r.status_code == 200, r.text
    assert r.json()["name"] == "jremote"
    assert writer.read_text().split("\n")[:-1] == ["tag", "rename", "jremot",
                                                   "jremote"]


def test_rewording_addresses_the_tag_by_its_current_name(client, writer):
    r = client.patch(_tags_url("jremote"), json={"description": "the app  and its host"})
    assert r.status_code == 200, r.text
    assert writer.read_text().split("\n")[:-1] == [
        "tag", "describe", "jremote", "--description", "the app  and its host"]


def test_a_rename_and_a_reword_together_reword_first(client, writer):
    """Both in one save is what an editor screen actually sends. `describe`
    addresses the tag by the name the caller sent, so it has to land before the
    rename moves that name — the other order describes a tag that is gone."""
    r = client.patch(_tags_url("jremot"),
                     json={"name": "jremote", "description": "the app"})
    assert r.status_code == 200, r.text
    # The recording stub keeps only its last argv; the rename running last is
    # what proves the order.
    assert writer.read_text().split("\n")[:-1] == ["tag", "rename", "jremot",
                                                   "jremote"]


def test_renaming_a_tag_to_itself_is_not_a_rename(client, writer):
    """A screen that saves without touching the name field sends it back
    unchanged. Shelling out anyway would make every save a write."""
    r = client.patch(_tags_url("jremote"), json={"name": "#JRemote"})
    assert r.status_code == 200
    assert not writer.exists()


def test_a_patch_that_changes_nothing_is_refused(client, writer):
    r = client.patch(_tags_url("jremote"), json={})
    assert r.status_code == 400
    assert not writer.exists()


def test_delete_defaults_to_the_gated_form(client, writer):
    """No `force` means the binary's own refusal stands — a carried tag says
    how many sittings it would unfile and declines. Sending `--force` by
    default would make the gate unreachable from the app."""
    r = client.request("DELETE", _tags_url("jremote"))
    assert r.status_code == 200, r.text
    assert writer.read_text().split("\n")[:-1] == ["tag", "delete", "jremote"]


def test_force_is_passed_through_only_when_asked(client, writer):
    r = client.request("DELETE", _tags_url("jremote") + "?force=true")
    assert r.status_code == 200, r.text
    assert writer.read_text().split("\n")[:-1] == ["tag", "delete", "jremote",
                                                   "--force"]


def test_the_carried_refusal_reaches_the_app_with_its_count(client, monkeypatch,
                                                            tmp_path):
    """The number is the whole value of the refusal: a confirmation dialog that
    cannot name what is lost is not a confirmation."""
    from jstack_host import board_watch
    monkeypatch.setattr(board_watch, "poke", lambda: None)
    binary, _ = _recording_bin(tmp_path, rc=2,
                               stderr="tag 'jremote' is carried by 12 sessions "
                                      "-- pass --force to delete it and unfile them")
    monkeypatch.setattr(timeline, "log_event_bin", lambda: binary)
    r = client.request("DELETE", _tags_url("jremote"))
    assert r.status_code == 400
    assert "carried by 12 sessions" in r.json()["detail"]


@pytest.mark.parametrize("call", [
    lambda c: c.post(_tags_url(), json={"name": "x", "description": "y"}),
    lambda c: c.patch(_tags_url("x"), json={"description": "y"}),
    lambda c: c.request("DELETE", _tags_url("x")),
])
def test_editing_on_a_host_with_no_timeline_is_503_not_a_quiet_ok(client, call,
                                                                  monkeypatch):
    monkeypatch.setattr(timeline, "log_event_bin", lambda: None)
    assert call(client).status_code == 503


@pytest.mark.parametrize("call", [
    lambda c: c.post(_tags_url(), json={"name": "x", "description": "y"}),
    lambda c: c.patch(_tags_url("x"), json={"description": "y"}),
    lambda c: c.request("DELETE", _tags_url("x")),
])
def test_every_vocabulary_edit_repaints_the_board(client, monkeypatch, tmp_path,
                                                  call):
    """A tag's name and description are drawn on cards. An edit the board never
    hears about leaves the old word on screen until something else pokes it."""
    from jstack_host import board_watch
    poked = []
    monkeypatch.setattr(board_watch, "poke", lambda: poked.append(True))
    binary, _ = _recording_bin(tmp_path)
    monkeypatch.setattr(timeline, "log_event_bin", lambda: binary)
    assert call(client).status_code == 200
    assert poked
