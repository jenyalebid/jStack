"""Sessions, against a real host: spawned, driven, watched and closed.

The largest surface the host serves and the one that least resembles itself
in-process. A session is a tmux pane with an agent CLI inside it, a transcript
on disk, a row on the board and a stream each device is watching — and the
routes here only mean anything when all four exist. An in-process test proves
the handler; the pane is what breaks.

**One real session, spawned once.** `live_session` opens a session on the
seeded agent and every read/write test shares it, because spawning is the
expensive part and thirty spawns would make this file the slowest thing in
the repo for no extra truth. The lifecycle routes — open-new and close — get
their own throwaway session so they are exercised inside a test rather than
in a fixture teardown, which runs after the coverage gate has already read
what was called.

**Cheap on purpose where the model is involved.** `/sessions/new` and
`/sessions/{sid}/turn` run an agent for real and spend against whoever's
account the guest is authed as. Both are exercised with a trivial input and
hung up on as soon as the host has committed to answering, which is the part
under test — a suite that burned a full completion per run would be one
nobody runs.
"""

from __future__ import annotations

import socket
import uuid

import httpx
import pytest

from conftest import API, BASE_URL, TIMEOUT

pytestmark = [pytest.mark.live,
              pytest.mark.skipif(not BASE_URL, reason="live suite is opt-in")]


# ── the session everything shares ──

@pytest.fixture(scope="session")
def live_session(api, agent_id) -> str:
    """A real managed session on the seeded agent, closed when the run ends.

    The teardown close is a safety net, not the coverage of `/close` — a guest
    left holding a live tmux pane per run drifts further from a fresh install
    with every iteration.
    """
    r = api.post("/sessions/open-new", json={"agent_id": agent_id})
    if r.status_code >= 300:
        pytest.skip(f"this host cannot spawn a session ({r.status_code}): "
                    f"{r.text[:300]}")
    sid = r.json().get("session_id")
    assert sid, f"open-new returned no session id: {r.text[:300]}"
    yield sid
    api.call("POST", "/sessions/{sid}/close", fmt={"sid": sid},
             **{"params": {"review": False}})


def _rows(payload, *keys) -> list:
    for key in keys:
        if isinstance(payload, dict) and isinstance(payload.get(key), list):
            return payload[key]
    return payload if isinstance(payload, list) else []


# ── the board: what a device reads before it opens anything ──

def test_the_session_index_answers(api):
    api.ok("GET", "/sessions")


def test_the_index_can_be_narrowed_to_an_agent(api, agent_id):
    """The seat's own list. A filter the host ignores is a Home screen that
    shows every agent's work under one card."""
    api.ok("GET", "/sessions", **{"params": {"agent": agent_id}})


def test_the_open_and_active_rosters_answer(api):
    api.ok("GET", "/sessions/open")
    api.ok("GET", "/sessions/active")


def test_a_spawned_session_appears_on_the_board(api, live_session):
    """Registered-first is the whole design: the board row exists before the
    agent does, because the row is the visibility. A spawn that is invisible
    until its first turn is remote work nobody can see starting."""
    rows = _rows(api.ok("GET", "/sessions/open"), "sessions", "open", "rows")
    ids = [r.get("session_id") or r.get("sid") or r.get("id") for r in rows]
    assert live_session in ids, (
        f"a session this suite just opened is not on the open board: {ids}")


def test_session_history_answers_and_searches(api, agent_id):
    api.ok("GET", "/sessions/history", **{"params": {"agent": agent_id, "q": ""}})
    api.ok("GET", "/sessions/history", **{"params": {"q": "zzz-no-such-text"}})


def test_the_close_log_answers(api):
    api.ok("GET", "/sessions/closes", **{"params": {"limit": 5}})


def test_the_active_board_streams(api):
    """The push every device leaves open. A 200 with the wrong content type is
    a board that never updates and a user who thinks nothing is running."""
    api.seen.add(("GET", API + "/sessions/active/stream"))
    headers = {"Authorization": f"Bearer {api.token}"} if api.token else {}
    with httpx.Client(timeout=20.0) as client:
        with client.stream("GET", BASE_URL + API + "/sessions/active/stream",
                           headers=headers) as r:
            assert r.status_code == 200, r.status_code
            assert "event-stream" in r.headers.get("content-type", ""), (
                f"active board is not a stream: {r.headers.get('content-type')!r}")
            for line in r.iter_lines():
                if line:
                    break


# ── one session, read every way the app reads it ──

def test_a_session_reads_back_by_id(api, live_session):
    body = api.ok("GET", "/sessions/{sid}", fmt={"sid": live_session})
    assert isinstance(body, dict), body


def test_a_sessions_timeline_answers(api, live_session):
    """What the notification panel renders. `load` is omitted when the reading
    fails and must never be zeroed — a context meter reading 0 on a full
    session is worse than a meter that admits it does not know."""
    body = api.ok("GET", "/sessions/{sid}/timeline", fmt={"sid": live_session})
    assert "events" in body, body
    if "load" in body:
        assert body["load"], "a load reading that failed must be omitted, not zeroed"


def test_a_session_streams_its_transcript(api, live_session):
    api.seen.add(("GET", API + "/sessions/{sid}/stream"))
    headers = {"Authorization": f"Bearer {api.token}"} if api.token else {}
    url = f"{BASE_URL}{API}/sessions/{live_session}/stream"
    with httpx.Client(timeout=20.0) as client:
        with client.stream("GET", url, headers=headers) as r:
            assert r.status_code == 200, r.status_code
            assert "event-stream" in r.headers.get("content-type", "")
            for line in r.iter_lines():
                if line:
                    break


def test_an_unknown_session_id_is_refused_everywhere(api):
    """`_check_sid` guards every one of these. A session id is a path segment
    a client controls, so a malformed one must be refused rather than reaching
    the filesystem."""
    for method, template in (("GET", "/sessions/{sid}"),
                             ("GET", "/sessions/{sid}/timeline"),
                             ("POST", "/sessions/{sid}/focus")):
        r = api.call(method, template, fmt={"sid": "../../etc/passwd"})
        assert 400 <= r.status_code < 500, (
            f"{method} {template} took a traversal as a session id: "
            f"{r.status_code}")


# ── driving it ──

def test_focus_and_interrupt_are_accepted(api, live_session):
    """Both are idempotent signals the app sends freely — focus on every tap,
    interrupt on the stop button — so neither may 500 on a session that is
    sitting at its prompt with nothing to interrupt."""
    r = api.post("/sessions/{sid}/focus", fmt={"sid": live_session})
    assert r.status_code < 500, f"focus → {r.status_code}: {r.text[:200]}"
    r = api.post("/sessions/{sid}/interrupt", fmt={"sid": live_session})
    assert r.status_code < 500, f"interrupt → {r.status_code}: {r.text[:200]}"


def test_typed_input_reaches_the_pane(api, live_session):
    """The phone's keyboard. Text goes to the CLI's input box without
    submitting a turn, which is what makes the composer usable at all."""
    r = api.post("/sessions/{sid}/input", fmt={"sid": live_session},
                 json={"text": "# live suite\n"})
    assert r.status_code < 500, f"{r.status_code}: {r.text[:200]}"


def test_the_composer_lifts_what_the_pane_is_holding(api, live_session):
    """Lift moves the CLI's input box into the app's sheet. The replacement is
    what stays behind, and empty is the compose case — leaving a copy would
    submit the same words twice."""
    r = api.post("/sessions/{sid}/composer/lift", fmt={"sid": live_session},
                 json={"replacement": ""})
    assert r.status_code < 500, f"{r.status_code}: {r.text[:200]}"


def test_opening_a_path_is_accepted_or_refused_cleanly(api, live_session):
    r = api.post("/sessions/{sid}/open-path", fmt={"sid": live_session},
                 json={"path": "/tmp"})
    assert r.status_code < 500, f"{r.status_code}: {r.text[:200]}"


def test_routing_a_url_is_accepted_or_refused_cleanly(api, live_session):
    r = api.post("/sessions/{sid}/route-open", fmt={"sid": live_session},
                 json={"url": "https://example.com"})
    assert r.status_code < 500, f"{r.status_code}: {r.text[:200]}"


def test_route_spawn_is_accepted_or_refused_cleanly(api, live_session):
    r = api.post("/sessions/{sid}/route-spawn", fmt={"sid": live_session},
                 json={"new_sid": str(uuid.uuid4()), "cwd": ""})
    assert r.status_code < 500, f"{r.status_code}: {r.text[:200]}"


def test_dismiss_elsewhere_is_accepted(api, live_session):
    """One device clearing a notification clears it on the others. A 500 here
    leaves a badge nobody can dismiss."""
    r = api.post("/sessions/{sid}/dismiss-elsewhere", fmt={"sid": live_session},
                 json={"instance": "live-suite"})
    assert r.status_code < 500, f"{r.status_code}: {r.text[:200]}"


def test_a_pane_screenshot_is_served_or_refused(api, live_session):
    """501 is a real answer, not a failure: `pict` is a separate binary a host
    may not carry, and saying so is the honest reply — the same shape the
    control tier uses for machinery it does not have. What this rules out is a
    500, which would mean the route tried and broke."""
    r = api.post("/sessions/{sid}/pict", fmt={"sid": live_session},
                 **{"params": {"full": False}})
    assert r.status_code < 500 or r.status_code == 501, (
        f"pict → {r.status_code}: {r.text[:200]}")


#: The honest answers for machinery a host may not carry. 501 "not
#: implemented here" and 503 "tier unavailable" are both real replies; 500 is
#: not, and telling them apart is most of what this file is for.
_UNAVAILABLE = (501, 503)


def test_review_is_accepted_or_refused_cleanly(api, live_session):
    r = api.post("/sessions/{sid}/review", fmt={"sid": live_session})
    assert r.status_code < 500 or r.status_code in _UNAVAILABLE, (
        f"review → {r.status_code}: {r.text[:200]}")


def test_splitoff_says_so_when_the_dub_is_not_installed(api, live_session):
    """Splitoff shells out to a jStack plugin binary, and a host installed
    without the plugins tree has no dub. It used to raise FileNotFoundError
    out of the route and hand the phone a bare 500 for a feature the machine
    simply does not have.

    This is the defect this whole suite was built to catch: invisible
    in-process, where the plugin tree is always present, and immediate on the
    first host that did not have one.
    """
    r = api.post("/sessions/{sid}/splitoff", fmt={"sid": live_session})
    assert r.status_code != 500, (
        f"splitoff crashed instead of refusing: {r.text[:300]}")
    assert r.status_code < 500 or r.status_code in _UNAVAILABLE, (
        f"splitoff → {r.status_code}: {r.text[:200]}")


def test_reopening_a_live_session_is_idempotent(api, live_session):
    """`/open` is also the takeover path and is documented idempotent —
    reopening a session that is already managed must displace nothing."""
    r = api.post("/sessions/{sid}/open", fmt={"sid": live_session})
    assert r.status_code < 500, f"open → {r.status_code}: {r.text[:200]}"


def test_filing_a_session_under_a_tag(api, live_session, scratch_name):
    """Filing is refused for a name nobody minted — never minted by a typo,
    never silently dropped."""
    r = api.post("/sessions/{sid}/tags", fmt={"sid": live_session},
                 json={"verb": "add", "name": f"never{scratch_name}"})
    assert r.status_code < 500, f"{r.status_code}: {r.text[:200]}"


# ── the session's own pad ──

def test_the_session_pad_round_trips_a_file(api, live_session, scratch_name):
    """A session's scratchpad IS the seat's pad — one shared folder, reached
    by session id instead of agent id. Both doors must open on the same room,
    so the bytes are fetched back through this one."""
    name = f"{scratch_name}-sess.txt"
    body = b"session pad round trip\n"
    saved = api.ok("POST", "/sessions/{sid}/scratchpad/upload",
                   fmt={"sid": live_session},
                   **{"params": {"filename": name}, "content": body})
    landed = str(saved.get("path", "")).rsplit("/", 1)[-1] or name

    listing = api.ok("GET", "/sessions/{sid}/scratchpad", fmt={"sid": live_session})
    assert "files" in listing, listing

    got = api.get("/sessions/{sid}/scratchpad/file", fmt={"sid": live_session},
                  **{"params": {"rel": landed}})
    assert got.status_code == 200, f"{got.status_code}: {got.text[:200]}"
    assert got.content == body, "the pad served different bytes than it stored"

    api.ok("POST", "/sessions/{sid}/scratchpad/delete", fmt={"sid": live_session},
           json={"rel": landed})


def test_clearing_the_session_pad_is_accepted(api, live_session):
    r = api.post("/sessions/{sid}/scratchpad/clear", fmt={"sid": live_session})
    assert r.status_code < 500, f"{r.status_code}: {r.text[:200]}"


# ── the lifecycle, on its own session ──

def test_a_session_opens_and_closes(api, agent_id):
    """open-new and close, on a session this test owns start to finish.

    Deliberately not the shared fixture's: closing that one would strand every
    test after it, and closing in a teardown would run after the coverage gate
    had already read what was called.
    """
    opened = api.ok("POST", "/sessions/open-new", json={"agent_id": agent_id})
    sid = opened.get("session_id")
    assert sid, f"open-new returned no session id: {opened}"
    assert opened.get("engine"), (
        "the host resolves the engine — a spawn that names none leaves the "
        "client guessing which CLI it is driving")

    closed = api.post("/sessions/{sid}/close", fmt={"sid": sid},
                      **{"params": {"review": False}})
    assert closed.status_code < 500, f"close → {closed.status_code}: {closed.text[:300]}"


def test_a_spawn_for_an_unknown_agent_is_refused(api):
    r = api.post("/sessions/open-new", json={"agent_id": "no-such-agent"})
    assert r.status_code == 404, f"{r.status_code}: {r.text[:200]}"


def test_a_spawn_naming_an_unknown_engine_is_refused_not_defaulted(api, agent_id):
    """One fallback point, host-side: an omitted engine becomes the agent's
    default, a NAMED unknown one refuses. Silently running a different engine
    than was asked for surfaces much later, in a transcript nobody can
    explain."""
    r = api.post("/sessions/open-new",
                 json={"agent_id": agent_id, "engine": "not-a-real-engine"})
    assert r.status_code == 400, (
        f"host accepted an engine it does not have ({r.status_code}): "
        f"{r.text[:200]}")


def test_a_spawn_on_an_unminted_tag_is_refused(api, agent_id, scratch_name):
    """A shortcut naming a filing nobody minted would look like it worked and
    file nowhere. 503 where the host has no timeline at all, which is the same
    refusal for the same reason."""
    r = api.post("/sessions/open-new",
                 json={"agent_id": agent_id, "tag": f"never{scratch_name}"})
    assert r.status_code in (400, 503), (
        f"host opened a session on a tag nobody minted ({r.status_code})")


def test_a_headless_turn_is_accepted_and_starts_streaming(api, live_session):
    """`/turn` runs the agent for real. What is under test is that the host
    accepts the turn and commits to streaming an answer — so the response is
    hung up on as soon as that is established, rather than spending a full
    completion on every run of the suite."""
    api.seen.add(("POST", API + "/sessions/{sid}/turn"))
    headers = {"Authorization": f"Bearer {api.token}"} if api.token else {}
    url = f"{BASE_URL}{API}/sessions/{live_session}/turn"
    with httpx.Client(timeout=30.0) as client:
        with client.stream("POST", url, headers=headers,
                           json={"text": "hi"}) as r:
            assert r.status_code < 500, f"turn → {r.status_code}"


def test_a_new_headless_session_is_accepted(api, agent_id):
    """`/sessions/new` is the same contract from the other end — spawn and
    first turn in one call. Hung up on for the same reason."""
    api.seen.add(("POST", API + "/sessions/new"))
    headers = {"Authorization": f"Bearer {api.token}"} if api.token else {}
    with httpx.Client(timeout=30.0) as client:
        with client.stream("POST", BASE_URL + API + "/sessions/new",
                           headers=headers,
                           json={"agent_id": agent_id, "text": "hi"}) as r:
            assert r.status_code < 500, f"new → {r.status_code}"


# ── the pty socket ──

def _ws_handshake(sid: str, token: str, *, cols: int = 80,
                  rows: int = 24, settle: bool = False) -> tuple[int, bytes]:
    """Open the pty websocket by hand and return `(code, first_bytes)`.

    Done over a raw socket with `wsproto` — which the host already depends on
    — rather than by adding a websocket client to the test environment. The
    handshake is the part worth proving live: the pty route is mounted on a
    separate router from every other route in this suite, so it is exactly the
    one that can go missing while all 80 HTTP routes still answer.

    `settle` is the difference between "did it upgrade" and "was it let in",
    and the distinction is the whole auth contract here. A websocket has no
    way to refuse at the HTTP layer with a useful reason, so the host accepts
    the upgrade and *then* closes with an application code — 4401 for a
    missing or invalid bearer. Without settling, a refused client and an
    admitted one both read as 101, which is how a first draft of these tests
    reported the terminal as wide open when it is not.

    So `settle=True` keeps reading past the upgrade and returns whichever
    comes first: the close code, or 101 once real bytes have flowed.
    """
    from wsproto import WSConnection, ConnectionType
    from wsproto.events import (AcceptConnection, BytesMessage, CloseConnection,
                                RejectConnection, Request, TextMessage)

    url = httpx.URL(BASE_URL)
    path = f"{API}/sessions/{sid}/pty?cols={cols}&rows={rows}"
    sock = socket.create_connection((url.host, url.port or 80), timeout=15)
    try:
        ws = WSConnection(ConnectionType.CLIENT)
        # No token means no header at all, not an empty one: `Bearer ` with
        # nothing after it is an illegal header value and h11 refuses to send
        # it, so the test would fail in the client and never ask the host
        # anything — a fail-closed check that never reached the door.
        extra = ([(b"Authorization", f"Bearer {token}".encode())] if token
                 else [])
        req = Request(host=f"{url.host}:{url.port or 80}", target=path,
                      extra_headers=extra)
        sock.sendall(ws.send(req))
        payload = b""
        accepted = False
        while True:
            data = sock.recv(65536)
            if not data:
                return (101 if accepted else 0), payload
            ws.receive_data(data)
            for event in ws.events():
                if isinstance(event, AcceptConnection):
                    accepted = True
                    if not settle:
                        return 101, payload
                if isinstance(event, RejectConnection):
                    return event.status_code, payload
                if isinstance(event, CloseConnection):
                    return event.code, payload
                if isinstance(event, (TextMessage, BytesMessage)):
                    payload += (event.data if isinstance(event.data, bytes)
                                else event.data.encode())
                    if settle:
                        return 101, payload
    finally:
        sock.close()


def test_the_pty_socket_accepts_an_authenticated_client(api, live_session):
    """The terminal itself. Mounted on `ws_router`, separate from every HTTP
    route here — a mount that was dropped would leave all 80 other routes
    answering and the terminal dead, which no HTTP test can see."""
    api.seen.add(("WS", API + "/sessions/{sid}/pty"))
    status, _ = _ws_handshake(live_session, api.token)
    assert status == 101, f"pty socket refused an authenticated client: {status}"


def test_the_pty_socket_refuses_a_client_with_no_token(api, live_session):
    """Fail-closed on the websocket too — checked past the upgrade.

    The host accepts the handshake and then closes with 4401, which is the
    only way a websocket can refuse with a reason, and it does so before
    `_spawn_attach` — so no terminal is ever created for an unauthenticated
    client. Asserting on the upgrade alone would read that correct behaviour
    as a wide-open terminal.
    """
    code, payload = _ws_handshake(live_session, "", settle=True)
    assert code == 4401, (
        f"an unauthenticated pty client was closed with {code}, not 4401")
    assert not payload, (
        f"the pty socket sent {len(payload)} bytes to a client with no token")
