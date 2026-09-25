"""Fixtures for the live suite — a real host, over a real socket.

**Why this is not under `tests/`.** That package's conftest installs three
autouse fixtures that point the device table, the open-session registry and
the session index at `tmp_path`, precisely so an in-process test can never
write into a running host. Everything here wants the opposite: the real store,
the real registry, the real machine. Inheriting that isolation would leave a
suite that looked live and tested a temp directory — the exact shape of a
check that lies, so the suite sits in its own directory instead of opting out
of fixtures it would be one `tests/conftest.py` edit away from re-inheriting.

**Nothing here runs by accident.** With no `JREMOTE_LIVE_URL` in the
environment every test skips, so `pytest tests/` and `pytest` from the repo
root are unchanged. `scripts/live-vm-test.sh` sets it, against a host it built
in a throwaway VM.

**Point it at a VM, not at this Mac.** The suite opens sessions, writes files
and revokes devices. `_guard_against_the_operators_mac` refuses a base URL
that resolves to this machine's own host unless `JREMOTE_LIVE_I_MEAN_IT=1`,
because the first accident anyone has here is running it against production
and wiping the board someone is actually looking at.
"""

from __future__ import annotations

import os
import socket
import uuid

import httpx
import pytest

#: Set by `scripts/live-vm-test.sh`. Absent = the whole suite skips.
BASE_URL = os.environ.get("JREMOTE_LIVE_URL", "").rstrip("/")
TOKEN = os.environ.get("JREMOTE_LIVE_TOKEN", "")
API = "/api/jremote/v1"

#: Long enough that a cold guest answering its first request is not a failure,
#: short enough that a hung route fails the suite instead of the wall clock.
TIMEOUT = 20.0


def _is_this_mac(base_url: str) -> bool:
    """Whether `base_url` points at the host running the tests.

    Compared by resolved address, not by string: `localhost`, `127.0.0.1`,
    this Mac's `.local` name and its LAN address are four spellings of the one
    machine, and a guard that only knew the first spelling is a guard that
    passes right up until it matters.
    """
    try:
        host = httpx.URL(base_url).host
    except Exception:  # noqa: BLE001 — an unparseable URL is not this Mac
        return False
    try:
        target = {info[4][0] for info in socket.getaddrinfo(host, None)}
    except OSError:
        return False
    if any(a.startswith("127.") or a == "::1" for a in target):
        return True
    try:
        mine = {info[4][0] for info in socket.getaddrinfo(socket.gethostname(), None)}
    except OSError:
        mine = set()
    return bool(target & mine)


def pytest_configure(config):
    config.addinivalue_line("markers", "live: runs against a real host over HTTP")


@pytest.fixture(scope="session", autouse=True)
def _guard_against_the_operators_mac():
    """Refuse to run destructively against the machine running the suite."""
    if not BASE_URL:
        return
    if _is_this_mac(BASE_URL) and os.environ.get("JREMOTE_LIVE_I_MEAN_IT") != "1":
        pytest.exit(
            f"JREMOTE_LIVE_URL={BASE_URL} resolves to this machine. This suite "
            "opens sessions, writes files and revokes devices. Point it at a "
            "VM (scripts/live-vm-test.sh), or set JREMOTE_LIVE_I_MEAN_IT=1.",
            returncode=3)


@pytest.fixture(scope="session")
def exercised() -> set[tuple[str, str]]:
    """Every `(METHOD, path-template)` the suite actually called.

    Recorded by `LiveAPI`, read by the coverage gate. A set rather than a
    count: "79 of 81" is a number nobody can act on, the two names are.
    """
    return set()


class LiveAPI:
    """An HTTP client that records the route template it just called.

    The recording is why every call names its template and passes path
    parameters separately — `api.get(f"/sessions/{sid}")` would be shorter and
    would record a URL that matches no route in the router, so the coverage
    gate could never tell a covered route from an uncovered one. The
    awkwardness is load-bearing.
    """

    def __init__(self, base_url: str, token: str, seen: set[tuple[str, str]]):
        self.base_url = base_url
        self.token = token
        self.seen = seen
        self._client = httpx.Client(base_url=base_url, timeout=TIMEOUT,
                                    follow_redirects=False)

    def close(self) -> None:
        self._client.close()

    def call(self, method: str, template: str, *, fmt: dict | None = None,
             token: str | None = ..., **kw) -> httpx.Response:
        """Call `template`, formatted with `fmt`, and record the template.

        `fmt` fills the route's `{placeholders}`; it is deliberately NOT named
        `params`, which httpx already owns for the query string. One name for
        both would have made `params={"path": ...}` on a route with no
        placeholders vanish into the formatter instead of reaching the wire —
        a test that passes while asking the host nothing.

        `token=None` sends no Authorization header — how the auth tests reach
        an unauthenticated request without building a second client.
        """
        path = template.format(**(fmt or {}))
        self.seen.add((method.upper(), API + template))
        headers = dict(kw.pop("headers", {}))
        bearer = self.token if token is ... else token
        if bearer:
            headers["Authorization"] = f"Bearer {bearer}"
        return self._client.request(method.upper(), API + path,
                                    headers=headers, **kw)

    def raw(self, method: str, path: str, **kw) -> httpx.Response:
        """Call a path that does not live under the API prefix, and record it.

        `/api/health` is the whole reason: it is a route the host serves, so
        the coverage gate counts it, and it is the one route that must answer
        before a token exists — so it cannot be reached through `call`, which
        prefixes every path and attaches a bearer. Without this the gate would
        report it untested while two tests were calling it with bare httpx.
        """
        self.seen.add((method.upper(), path))
        headers = dict(kw.pop("headers", {}))
        return self._client.request(method.upper(), path, headers=headers, **kw)

    def get(self, template, **kw):
        return self.call("GET", template, **kw)

    def post(self, template, **kw):
        return self.call("POST", template, **kw)

    def patch(self, template, **kw):
        return self.call("PATCH", template, **kw)

    def delete(self, template, **kw):
        return self.call("DELETE", template, **kw)

    def ok(self, method: str, template: str, **kw) -> dict | list:
        """Call and insist on a 2xx, with the body in the failure message.

        A live suite whose failures read `assert 500 == 200` costs a second
        run to find out what the host said; the host already said it.
        """
        r = self.call(method, template, **kw)
        assert r.status_code < 300, (
            f"{method} {template} → {r.status_code}: {r.text[:400]}")
        try:
            return r.json()
        except ValueError:
            return {}


@pytest.fixture(scope="session")
def api(exercised) -> LiveAPI:
    if not BASE_URL:
        pytest.skip("no JREMOTE_LIVE_URL — live suite is opt-in "
                    "(scripts/live-vm-test.sh)")
    client = LiveAPI(BASE_URL, TOKEN, exercised)
    yield client
    client.close()


@pytest.fixture(scope="session")
def host_identity(api) -> dict:
    """`/host`, fetched once. Most tests need the host id or its mode, and 40
    tests each re-asking is 40 round trips for one unchanging answer."""
    return api.ok("GET", "/host")


@pytest.fixture(scope="session")
def agent_id(api) -> str:
    """An agent this host actually serves — the subject the roster, the tree,
    the Files pane and every session route need.

    Read off `/agents` rather than hardcoded: the suite runs against a guest
    the runner seeded, but it must also be pointable at any host, and a fixed
    id would make it a suite about one machine. A host with no agents skips
    rather than fails — `~/Agents` absent is a correct answer for a fresh
    install (`hostenv.instance_root`), not a defect, and the runner is what
    guarantees the guest has one.
    """
    roster = api.ok("GET", "/agents")
    agents = roster.get("agents", []) if isinstance(roster, dict) else roster
    ids = [a.get("agent_id") or a.get("id") for a in agents]
    ids = [i for i in ids if i]
    if not ids:
        pytest.skip("this host resolves no agents — scripts/live-vm-test.sh "
                    "seeds one; a bare host has nothing for these routes to "
                    "be about")
    return ids[0]


@pytest.fixture
def scratch_name() -> str:
    """A name no previous run can collide with.

    The suite runs against a VM that is not always thrown away between runs —
    `vm.sh reset` is a second, but a developer iterating will skip it — so
    every artifact this suite creates carries a fresh id rather than a fixed
    one that a rerun would trip over.
    """
    return f"live-{uuid.uuid4().hex[:8]}"
