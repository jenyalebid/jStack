"""The host API imports on a machine that has never had the embedding tree.

This is the whole point of the seam. The package is meant to be lifted into the
jRemote repo and run beside the app on any Mac — but nothing about that failure
is visible from here, because on this machine every import it must not need
happens to succeed. So the check has to *remove* them and try.

What "standalone" means, exactly: `lib` (the embedding tree's own libraries) and every
dashboard package except `jremote` itself are unimportable. A module that still
comes up under those conditions carries no hidden dependency on this Mac.

One import is deliberately exempt and is asserted as such below: `lib.agents`
reached through `hostenv`, which is the seam and is designed to be absent.
"""

import subprocess
import sys
from pathlib import Path

import pytest

from conftest import needs_embedding_tree
from jstack_host import hostenv as _hostenv

# The package itself, asked of the package. This spelled the old tree
# layout literally, so after the move it pointed at a directory that does
# not exist and the read failed before the assertion could run.
PKG = Path(_hostenv.__file__).resolve().parent
# Where the package imports from — asked of the package, not counted in
# parent directories. `parents[N]` keeps working right up until the tree
# moves and then picks a directory that merely exists.
INFRA = _hostenv.package_root()

#: Everything a standalone host would not have. `jstack_host` is absent
#: from this list — it is the thing under test.
BLOCKED = (
    "lib",
    "dashboard.shared", "dashboard.routes", "dashboard.pages", "dashboard.app",
    "dashboard.config", "dashboard.auth", "dashboard.issue_work",
    "dashboard.github_client", "dashboard.state",
)

_PROBE = '''
import importlib, sys

BLOCKED = {blocked!r}

class Blocker:
    """Refuse anything a standalone host would not have."""
    def find_spec(self, name, path=None, target=None):
        for b in BLOCKED:
            if name == b or name.startswith(b + "."):
                raise ImportError("not on a standalone host: " + name)
        return None

sys.meta_path.insert(0, Blocker())
importlib.import_module("jstack_host.{mod}")
'''


def _modules():
    return sorted(p.stem for p in PKG.glob("*.py") if p.stem != "__init__")


def _import_standalone(mod: str):
    """Import one module in a fresh interpreter with the embedding tree removed."""
    return subprocess.run(
        [sys.executable, "-c", _PROBE.format(blocked=BLOCKED, mod=mod)],
        cwd=INFRA, capture_output=True, text=True)


@pytest.mark.parametrize("mod", _modules())
def test_module_imports_without_the_jj_tree(mod):
    """Every module, one subprocess each.

    Per-module rather than one pass over the package: an import chain reports
    only its first casualty, so a single failing leaf hides every module that
    imports it. Parametrized so the failure names the module that is actually
    coupled, not the first one alphabetically to notice.
    """
    r = _import_standalone(mod)
    if r.returncode != 0:
        blocked = [ln.split("not on a standalone host: ")[-1]
                   for ln in r.stderr.splitlines()
                   if "not on a standalone host: " in ln]
        raise AssertionError(
            f"jstack_host.{mod} needs {blocked[-1] if blocked else '?'} "
            f"at import time — route it through hostenv, move it into the "
            f"package, or import it lazily inside the function that needs it."
            f"\n\n{r.stderr[-1500:]}")


def test_the_seam_asks_the_machine_by_name_and_never_imports_lib():
    """The seam resolves the machine's profile module by convention
    (`jremote_host_profile`, via importlib); it never imports this repo's
    `lib` itself.

    Stated as a test because the rule is otherwise invisible: the package is a
    product, and the machine-specific profile is something a machine
    *supplies*, never something the package carries. A future reader adding a
    convenient `from lib import …` back into hostenv would re-couple every
    shipped host to this Mac.
    """
    src = (PKG / "hostenv.py").read_text()
    assert "from lib import" not in src and "import lib" not in src, \
        "hostenv reaches into this repo's lib — the profile module owns that"
    assert "jremote_host_profile" in src, \
        "hostenv no longer resolves the machine's profile module by convention"
    r = _import_standalone("hostenv")
    assert r.returncode == 0, "hostenv must import with lib absent, not raise"


def test_a_standalone_host_answers_with_the_default_profile():
    """Importing is not enough — the roster has to resolve to something usable.

    A package that imports and then raises on the first call is not portable,
    it is quietly broken. This runs the profile resolution itself with `lib`
    gone and requires real answers out the other side.
    """
    probe = _PROBE.format(blocked=BLOCKED, mod="hostenv") + '''
import os, tempfile
from pathlib import Path
root = Path(tempfile.mkdtemp()) / "Agents"
(root / "Nova" / "chat").mkdir(parents=True)
os.environ["JREMOTE_INSTANCE_ROOT"] = str(root)
from jstack_host import hostenv
hostenv.reset_profile()
assert hostenv.profile().name == "default", hostenv.profile().name
assert set(hostenv.active_agents()) == {"nova"}, hostenv.active_agents()
assert hostenv.workspace("nova-chat") == root / "Nova" / "chat"
enc = str(root).replace("/", "-") + "-Nova-chat"
assert hostenv.project_dir_to_agent(enc) == ("nova", "chat")
assert hostenv.default_model()
assert "/usr/bin" in hostenv.spawn_path()
assert hostenv.project_dir_agent_overrides() == {}
assert hostenv.session_labels() == {}
print("OK")
'''
    r = subprocess.run([sys.executable, "-c", probe], cwd=INFRA,
                       capture_output=True, text=True)
    assert r.returncode == 0 and "OK" in r.stdout, r.stderr[-2000:]


def test_a_standalone_host_can_actually_scan_its_processes():
    """The process scan is the one moved module that reads a host fact mid-call.

    Importing `procscan` with `lib` gone proves nothing about the scan itself:
    the labels come through `hostenv` at call time, and the embedding path behind that
    seam reaches for `dashboard.shared.helpers` — a module a standalone host
    does not have. So the scan is *run*, not just imported, and its rows are
    required to come out shaped the way the board reads them.
    """
    probe = _PROBE.format(blocked=BLOCKED, mod="procscan") + '''
from jstack_host import hostenv, procscan
hostenv.reset_profile()
assert hostenv.profile().name == "default", hostenv.profile().name
out = procscan.get_claude_processes()
assert set(out) == {"processes", "count"}, out.keys()
assert out["count"] == len(out["processes"])
for row in out["processes"]:
    assert row["engine"] in ("claude", "codex"), row
    assert row["label"] == "" or isinstance(row["label"], str)
assert isinstance(procscan._tty_map(), dict)
print("OK")
'''
    r = subprocess.run([sys.executable, "-c", probe], cwd=INFRA,
                       capture_output=True, text=True)
    assert r.returncode == 0 and "OK" in r.stdout, r.stderr[-2000:]


# ── The availability contract ──

#: The screens drawn from the embedding tree's own machinery, and the key each one carries its
#: rows in. A host without the tree must answer all of them — shape intact, body
#: empty, `available: false` — rather than 500.
# The screens a host without the embedding tree genuinely cannot have. The feed,
# spend and allowance used to sit here too; they are served by the package's
# own readers now (`feed`, `spend`, `allowance`), off what any jStack machine
# records — see `test_the_portable_screens_answer_on_a_tree_less_host`.
OPTIONAL_SCREENS = [
    ("context", "skills"),
]


def _serve_standalone(body: str, extra_env: str = ""):
    """Run `body` against a real TestClient of the host, embedding tree removed."""
    probe = f'''
import importlib, sys

BLOCKED = {BLOCKED!r}

class Blocker:
    def find_spec(self, name, path=None, target=None):
        for b in BLOCKED:
            if name == b or name.startswith(b + "."):
                raise ImportError("not on a standalone host: " + name)
        return None

sys.meta_path.insert(0, Blocker())

import os, tempfile
from pathlib import Path
state = Path(tempfile.mkdtemp())
(state / "api-token").write_text("probe-token")
root = state / "Agents"
(root / "Nova" / "chat").mkdir(parents=True)
os.environ["JREMOTE_INSTANCE_ROOT"] = str(root)
os.environ["JREMOTE_STATE_DIR"] = str(state)
# A tmux socket nothing else is on. Nothing here spawns or closes a session,
# but the modules reach for tmux, and they will not reach for the user's.
os.environ["JREMOTE_TMUX_SOCK"] = "jr-pytest-standalone"
{extra_env}

from fastapi.testclient import TestClient
from jstack_host import hostenv, server
hostenv.reset_profile()
assert hostenv.profile().name == "default", hostenv.profile().name

# No `with`: the lifespan reconciles tmux and starts indexers, and this test is
# about what the routes answer, not about the daemon coming up.
client = TestClient(server.create_app())
AUTH = {{"Authorization": "Bearer probe-token"}}
{body}
print("OK")
'''
    return subprocess.run([sys.executable, "-c", probe], cwd=INFRA,
                          capture_output=True, text=True)


def test_a_host_without_the_tree_says_so_instead_of_failing():
    """Absence is an answer. The dashboard's own screens, honest about being absent.

    The failure this prevents is not a crash — it is a lie. An empty feed on a
    host that *has* no feed reads exactly like a quiet afternoon, and an empty
    caps meter reads like nothing has been spent. So each endpoint is required
    to return its normal shape, empty, carrying `available: false`, and the
    emptiness is asserted alongside the flag: a screen that reported absent and
    then handed back rows anyway would be its own kind of wrong.
    """
    checks = "\n".join(
        f'''
r = client.get("/api/jremote/v1/{path}", headers=AUTH)
assert r.status_code == 200, ("{path}", r.status_code, r.text[:300])
d = r.json()
assert d.get("available") is False, ("{path} must report absence", d)
assert d.get("reason"), ("{path} must say why", d)
assert d["{key}"] == [] or d["{key}"] == {{}}, ("{path} must be empty", d["{key}"])
'''
        for path, key in OPTIONAL_SCREENS)
    r = _serve_standalone(checks)
    assert r.returncode == 0 and "OK" in r.stdout, r.stderr[-3000:]


def test_the_portable_screens_answer_on_a_tree_less_host():
    """The feed and the spend scanner are the package's own, so a host with
    nothing but Claude Code and jStack serves them — with their real shape,
    `available` absent or true, and never the dashboard's reason string. An
    empty day is a fine answer here; a greyed-out screen is the bug."""
    r = _serve_standalone('''
r = client.get("/api/jremote/v1/feed?date=2026-01-01", headers=AUTH)
assert r.status_code == 200, (r.status_code, r.text[:300])
d = r.json()
assert d.get("available", True) is True, d
assert d["date"] == "2026-01-01" and isinstance(d["events"], list), d
ids = [s["id"] for s in d["sources"]]
assert ids == ["timeline", "session", "commit", "run", "message", "ping"], ids
assert all(s["ok"] is True for s in d["sources"]), d["sources"]

r = client.get("/api/jremote/v1/usage/spend?days=3", headers=AUTH)
assert r.status_code == 200, (r.status_code, r.text[:300])
d = r.json()
assert d.get("available", True) is True, d
assert d["day"] and isinstance(d["total"], int) and len(d["series"]) == 3, d
assert all("categories" in s for s in d["series"]), d["series"]

r = client.get("/api/jremote/v1/usage/caps", headers=AUTH)
assert r.status_code == 200, (r.status_code, r.text[:300])
d = r.json()
# Either a real reading or an honest absence — never a zeroed meter.
if d.get("available", True):
    assert set(d["providers"]) == {"claude", "codex"}, d
    assert any(d["providers"].values()), d
    assert all(p["windows"] for p in d["providers"].values() if p), d
else:
    assert d["providers"] == {}, d
''')
    assert r.returncode == 0 and "OK" in r.stdout, r.stderr[-3000:]


def test_a_host_without_the_tree_still_serves_its_own_board():
    """The absent screens must not take the present ones down with them.

    `/sessions` is the host's own answer — tmux, transcripts, the process scan —
    and owes nothing to `lib`. If the availability guards were wired wrong, the
    obvious way for it to show is the board going empty or 500 on the machine
    the whole exercise exists to support.
    """
    r = _serve_standalone('''
r = client.get("/api/jremote/v1/sessions", headers=AUTH)
assert r.status_code == 200, (r.status_code, r.text[:300])
assert "sessions" in r.json(), r.json().keys()

h = client.get("/api/health")
assert h.status_code == 200, h.status_code
assert h.json()["standalone"] is True and h.json()["profile"] == "default", h.json()
assert h.json()["provisioned"] is True, h.json()
''')
    assert r.returncode == 0 and "OK" in r.stdout, r.stderr[-3000:]


def test_the_unauthenticated_probe_is_the_only_unauthenticated_route():
    """`/api/health` is open so the app can find a host before it has a token.

    That is a deliberate hole and it stays exactly one hole wide: everything
    else answers 401 without a bearer token. Checked by walking the app's own
    route table rather than a hand-kept list, so a route added later is covered
    by this test the day it lands.
    """
    r = _serve_standalone('''
from starlette.routing import Route
opened = []
for route in server.create_app().routes:
    if not isinstance(route, Route) or "GET" not in (route.methods or ()):
        continue
    if "{" in route.path:
        continue                       # needs an id we would have to invent
    if client.get(route.path).status_code != 401:
        opened.append(route.path)
assert opened == ["/api/health"], opened
''')
    assert r.returncode == 0 and "OK" in r.stdout, r.stderr[-3000:]


# ── Where the host keeps its state ──

#: Every module that names a file in the state dir, and the attribute it binds
#: it to. Bound at import as a plain constant, so what is really under test is
#: that `hostenv` was consulted rather than the path being rebuilt by hand.
STATE_BINDINGS = [
    ("board", "_TURN_DIR"), ("board", "_ATTENTION_DIR"),
    ("engines", "_STATE"), ("events", "_FILE"), ("managed", "_REG"),
    ("notify", "_STATE"), ("store", "_STATE_DIR"), ("store", "DB_PATH"),
]


@needs_embedding_tree
def test_this_mac_keeps_every_state_file_exactly_where_it_was():
    """The centralisation must be a no-op here. No migration, nothing stranded.

    Routing nine hand-built paths through one seam is worth nothing if the seam
    answers differently than the expressions it replaced — that would silently
    move the user's live board, device registrations and session index to a fresh
    empty directory, and the symptom would be a working dashboard with no
    history rather than an error.
    """
    # A clean interpreter with every JREMOTE_* override scrubbed. Not because
    # the imports are slow to undo but because they cannot be: the constants
    # bind once, so a sibling test that pointed the state dir at its tmp_path
    # has already decided the answer for anything running in this process.
    probe = '''
import os
from pathlib import Path
INFRA = Path(%r)
for k in [k for k in os.environ if k.startswith("JREMOTE_")]:
    del os.environ[k]
from jstack_host import hostenv
assert hostenv.profile().name == "jj", hostenv.profile().name
state = INFRA / "dashboard" / "state"
assert hostenv.state_dir() == state, hostenv.state_dir()

import importlib
for mod, attr in %r:
    m = importlib.import_module("jstack_host." + mod)
    got = Path(getattr(m, attr))
    # `==` for the one binding that is the dir itself (store._STATE_DIR).
    assert got == state or state in got.parents, \\
        mod + "." + attr + " escaped the state dir: " + str(got)

assert hostenv.token_path() == INFRA / "Credentials" / "jremote-api-token", hostenv.token_path()
assert hostenv.releases_dir() == INFRA / "state" / "jremote-releases" / "mac", hostenv.releases_dir()
print("OK")
''' % (str(INFRA), STATE_BINDINGS)
    r = subprocess.run([sys.executable, "-c", probe], cwd=INFRA,
                       capture_output=True, text=True)
    assert r.returncode == 0 and "OK" in r.stdout, r.stderr[-3000:]


def test_naming_a_state_dir_moves_the_whole_host_including_its_token():
    """`--state-dir` has to isolate a host completely or it isolates nothing.

    This caught a real bug: the profile built its token path from its *own*
    default state dir rather than the effective one, so a second host moved its
    board, sessions and devices and went on reading the first host's token —
    two hosts, one credential, no error anywhere. Asserted on the token
    specifically for that reason, not just on the directory.
    """
    r = _serve_standalone('''
import os
state = Path(os.environ["JREMOTE_STATE_DIR"])
assert hostenv.state_dir() == state, hostenv.state_dir()
assert hostenv.token_path() == state / "api-token", hostenv.token_path()

from jstack_host import managed, store, notify
for got in (Path(managed._REG), Path(store.DB_PATH), Path(notify._STATE)):
    assert state in got.parents, got

# The one thing that must NOT follow, and its override.
os.environ["JREMOTE_TOKEN_PATH"] = "/tmp/elsewhere/tok"
assert hostenv.token_path() == Path("/tmp/elsewhere/tok"), hostenv.token_path()
''')
    assert r.returncode == 0 and "OK" in r.stdout, r.stderr[-3000:]


def test_reading_the_state_dir_does_not_create_it():
    """Imports have no side effects. Asking where state lives is not a decision.

    Eight modules resolve a state path at import. If the lookup created the
    directory, importing the package anywhere — a test, a lint pass, a `-c`
    one-liner with a stale env var — would litter the filesystem with empty
    jRemote state dirs, and one of them would eventually be the one a host came
    up pointed at. `ensure_state_dir()` is the create, and the host calls it.
    """
    probe = f'''
import os, tempfile
from pathlib import Path
target = Path(tempfile.mkdtemp()) / "never-created"
os.environ["JREMOTE_STATE_DIR"] = str(target)
from jstack_host import hostenv, board, store, managed   # noqa: F401
assert hostenv.state_dir() == target, hostenv.state_dir()
assert not target.exists(), "importing the package created the state dir"
assert hostenv.ensure_state_dir() == target and target.is_dir()
print("OK")
'''
    r = subprocess.run([sys.executable, "-c", probe], cwd=INFRA,
                       capture_output=True, text=True)
    assert r.returncode == 0 and "OK" in r.stdout, r.stderr[-2000:]


def test_two_hosts_cannot_share_one_state_dir():
    """The second host is refused, and told the one thing it needs to hear.

    Two hosts on one state dir is two reapers closing each other's sessions and
    two indexers writing one SQLite file — and on this Mac the first host is the
    dashboard, so the collision is the default outcome of running the standalone
    server here rather than an exotic one. The lock is `flock`, not a pid file,
    so a `kill -9` leaves nothing stale to clear by hand: the second half of
    this test takes the lock in a child, kills it dead, and requires the next
    host to start cleanly.
    """
    probe = '''
import os, signal, subprocess, sys, tempfile, time
from pathlib import Path
state = Path(tempfile.mkdtemp())
os.environ["JREMOTE_STATE_DIR"] = str(state)
from jstack_host import hostenv, server
hostenv.ensure_state_dir()

held = server.acquire_lock()
try:
    server.acquire_lock()
except SystemExit as e:
    assert str(os.getpid()) in str(e), e
    assert "--state-dir" in str(e), "the refusal must say how to proceed"
else:
    raise AssertionError("a second host took a lock that was already held")
held.close()
assert server.acquire_lock(), "the lock must be retakeable once released"

# Now the hard way: a holder that is killed outright.
child = subprocess.Popen(
    [sys.executable, "-c",
     "import os,sys,time;"
     "sys.path.insert(0, {infra!r});"
     "os.environ['JREMOTE_STATE_DIR']=" + repr(str(state)) + ";"
     "from jstack_host import server;"
     "server.acquire_lock();print('held',flush=True);time.sleep(60)"],
    stdout=subprocess.PIPE, text=True)
assert child.stdout.readline().strip() == "held"
child.kill(); child.wait()
for _ in range(50):
    try:
        server.acquire_lock(); break
    except SystemExit:
        time.sleep(0.1)
else:
    raise AssertionError("a killed host left its lock behind")
print("OK")
'''.format(infra=str(INFRA))
    r = subprocess.run([sys.executable, "-c", probe], cwd=INFRA,
                       capture_output=True, text=True)
    assert r.returncode == 0 and "OK" in r.stdout, r.stderr[-3000:]


# ── Who this host is ──

def test_a_host_knows_which_machine_it_is_and_what_it_can_do():
    """`/host` is what the grid draws a machine from before entering it.

    Two claims, both load-bearing. The id must be **stable across processes** —
    a fresh id per restart would make the app's list of instances grow a row
    every time a host came back up. And `features` must **agree with the routes
    themselves**: a grid that says a host has a feed, opening onto a screen that
    reports it absent, is worse than either answer alone.
    """
    r = _serve_standalone('''
h = client.get("/api/jremote/v1/host", headers=AUTH)
assert h.status_code == 200, (h.status_code, h.text[:300])
d = h.json()
assert d["host_id"] and len(d["host_id"]) >= 32, d
assert d["name"], d
assert d["profile"] == "default", d

# What a host with no tree has and has not. `context` and `control` are the
# dashboard's own and must read absent. The feed and the spend scanner are
# the package's now, drawing on Claude Code's files and jStack's stores, so
# a tree-less host HAS them. The allowance depends on whether Claude Code has
# cached a reading on this machine, and `tags` on whether jStack's log_event
# is installed — both are probed off the filesystem, so they are asserted
# present in the map and consistent with their routes below, never as a
# fixed value the machine running this test would make a lie.
# Mode rides here too — one of the three, with a note and a liveness flag.
# Asserted by shape, not value: `mode.current()` reads the real machine's
# interfaces, so the running host's own network would make any fixed value a
# lie. The value rules are pinned in test_jremote_mode.py against fixed facts.
assert d["mode"]["mode"] in {"local", "open", "managed"}, d["mode"]
assert d["mode"]["note"], d["mode"]
assert isinstance(d["mode"]["live"], bool), d["mode"]

feats = d["features"]
assert feats["context"] is False and feats["control"] is False, feats
assert feats["feed"] is True and feats["usage_spend"] is True, feats
assert "usage_caps" in feats and "tags" in feats, feats

# The summary and the routes are the same answer — every optional screen,
# however it is probed. This is the claim that matters: a grid saying a host
# has a feature, opening onto a screen that reports it absent, is worse than
# either answer alone.
for key, path in [("context", "context"), ("usage_caps", "usage/caps"),
                  ("usage_spend", "usage/spend"), ("feed", "feed"),
                  ("tags", "tags")]:
    body = client.get("/api/jremote/v1/" + path, headers=AUTH).json()
    assert body.get("available", True) is d["features"][key], (key, body.get("available"))

# Stable: a second reader in this same state dir must see the same id.
from jstack_host import hostenv as he2
assert he2.host_id() == d["host_id"], (he2.host_id(), d["host_id"])
assert (Path(os.environ["JREMOTE_STATE_DIR"]) / "host-id").read_text().strip() == d["host_id"]
''')
    assert r.returncode == 0 and "OK" in r.stdout, r.stderr[-3000:]


def test_a_leaf_says_it_cannot_pair_instead_of_erroring_on_a_missing_file():
    """A leaf, 2026-09-03: Settings → Remote Access showed a red

        cannot read the pairing script at
        ~/.local/share/jremote/app/scripts/wireguard/wg_peer.py

    on a machine that reaches the mesh through a leaf tunnel and needs no peer
    of its own. Only the hub ships `wg_peer.py`, so the shipped payload never
    had that file and never should. Two claims, together: `/host` must declare
    the capability absent so the app can stop offering the button, and the
    route must answer **503** — unsupported here, not broken here — with a
    reason that names the arrangement rather than a path to go looking for.
    """
    r = _serve_standalone('''
from pathlib import Path
from jstack_host import tunnel
tunnel.PEER_SCRIPT = Path("/nonexistent/scripts/wireguard/wg_peer.py")

d = client.get("/api/jremote/v1/host", headers=AUTH).json()
assert d["features"]["tunnel_pairing"] is False, d["features"]

r = client.post("/api/jremote/v1/tunnel/pair",
                json={"device": "my-macbook-pro"}, headers=AUTH)
assert r.status_code == 503, (r.status_code, r.text[:300])
detail = r.json()["detail"]
assert "wg_peer.py" not in detail and "nonexistent" not in detail, detail
assert "does not run the tunnel" in detail, detail
''')
    assert r.returncode == 0 and "OK" in r.stdout, r.stderr[-3000:]


def test_the_hub_still_offers_pairing():
    """The mirror of the leaf test — the capability is not off for everyone.

    A flag that reads false everywhere would 'fix' that leaf by removing
    off-LAN pairing from the one machine that has it.

    Skipped where this checkout is not a configured hub. The tooling itself now
    ships with the package (`wg_peer.py` and its scripts are in the tree), so its
    presence no longer tells a hub from a leaf — what does is `wg0.conf`, the
    `sudo`-created interface a machine has only after `install_hub.sh` ran on it.
    `can_pair()` answering false for a checkout that never became a hub is the
    documented behaviour the leaf test pins; asserting the hub case there would
    fail for the one reason that is not a bug.
    """
    from jstack_host import router, tunnel
    if not tunnel.HUB_CONF.is_file():
        pytest.skip(f"not a configured hub — no wg0.conf at {tunnel.HUB_CONF}")
    assert router._probe("tunnel_pairing") is True


def test_two_hosts_on_one_machine_are_two_identities():
    """The whole reason the id exists — and the bug it prevents.

    Local-first routing wants to prefer `127.0.0.1` whenever the Mac the app is
    running on is itself a host. But an app configured for the *work* Mac and
    running on the dashboard Mac would then draw the local board instead: every
    session real, none of them the ones asked for, and nothing on screen
    wrong-looking enough to notice. Two state dirs on one machine must therefore
    be two ids — same hostname, same everything else.

    Also pins that the id does not follow the *machine*: `JREMOTE_HOST_ID`
    overrides, so a host restored from a backup onto new hardware stays the
    instance the app already knows rather than appearing as a stranger.
    """
    probe = '''
import os, tempfile
from pathlib import Path
from jstack_host import hostenv

a, b = Path(tempfile.mkdtemp()), Path(tempfile.mkdtemp())
os.environ["JREMOTE_STATE_DIR"] = str(a)
first = hostenv.host_id()
os.environ["JREMOTE_STATE_DIR"] = str(b)
second = hostenv.host_id()
assert first != second, "two hosts on one machine claimed one identity"
assert hostenv.host_name(), "a host must always have something to be called"

# Re-reading either one is stable, in any order.
os.environ["JREMOTE_STATE_DIR"] = str(a)
assert hostenv.host_id() == first, "the id must not change on re-read"

os.environ["JREMOTE_HOST_ID"] = "carried-over"
assert hostenv.host_id() == "carried-over", hostenv.host_id()
del os.environ["JREMOTE_HOST_ID"]

os.environ["JREMOTE_HOST_NAME"] = "  Laptop  "
assert hostenv.host_name() == "Laptop", repr(hostenv.host_name())
print("OK")
'''
    r = subprocess.run([sys.executable, "-c", probe], cwd=INFRA,
                       capture_output=True, text=True)
    assert r.returncode == 0 and "OK" in r.stdout, r.stderr[-3000:]
