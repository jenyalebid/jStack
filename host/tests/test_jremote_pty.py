"""PTY endpoint unit surface — the EOF disposition and attach refusals.

The socket's close code is the app's whole signal for "what just ended":
1000 must mean a session still standing (reconnectable detach), 4411 must
mean the session itself died (the app closes the thread on it).

Attaching is viewing, and a viewer never creates what it views: no attach,
however it is labelled, may stand a session up. Resuming is `POST
/sessions/{sid}/open`, which every client path that means to resume already
calls first. The revive that used to live here respawned sessions the user
had closed from another device — a stale board row was enough to trigger it.
"""

import pytest

from jstack_host import pty as jpty


def test_eof_with_session_alive_is_a_detach(monkeypatch):
    monkeypatch.setattr(jpty.managed, "is_open", lambda sid: True)
    assert jpty._eof_disposition("abc12345") == (1000, "detached")


def test_eof_with_session_gone_is_session_ended(monkeypatch):
    monkeypatch.setattr(jpty.managed, "is_open", lambda sid: False)
    assert jpty._eof_disposition("abc12345") == (4411, "session ended")


def _dead_but_known(monkeypatch):
    """sid has a transcript and no live holder anywhere — the old revive case."""
    monkeypatch.setattr(jpty.managed, "is_open", lambda sid: False)
    monkeypatch.setattr("jstack_host.transcripts._find_session_cwd",
                        lambda sid: "/tmp/somewhere")
    monkeypatch.setattr("jstack_host.board._live_session_ids",
                        lambda: set())


def _forbid_spawn(monkeypatch):
    """Any attempt to stand a session up is the regression — record it."""
    spawned = []
    monkeypatch.setattr(jpty.managed, "record_open",
                        lambda *a, **k: spawned.append(("record", a)))
    monkeypatch.setattr(jpty.managed, "open_managed",
                        lambda *a, **k: spawned.append(("open", a)))
    return spawned


def test_attach_to_a_dead_session_is_session_ended_not_a_revive(monkeypatch):
    """The 2026-09-03 respawn, as a test.

    A thread opened on a board row that went stale — the session was closed
    from another device in the gap — reaches here with a sid that has a
    transcript, a cwd and no live holder. That used to be the revive case, and
    the user got back the session they had just ended. It is a refusal now,
    whatever intent the client claimed: the app drops its stale row on 4411,
    so the board heals instead of resurrecting.
    """
    _dead_but_known(monkeypatch)
    spawned = _forbid_spawn(monkeypatch)
    with pytest.raises(jpty.SessionEnded):
        jpty._ensure_managed("abc12345")
    assert spawned == []


def test_attach_to_open_session_attaches(monkeypatch):
    monkeypatch.setattr(jpty.managed, "is_open", lambda sid: True)
    jpty._ensure_managed("abc12345")  # no raise = attach


def test_attach_to_a_session_with_no_transcript_is_ended_not_unknown(monkeypatch):
    """An attach never answers "unknown sid" for a session that left no file.

    The two are indistinguishable on disk: a session closed before Claude Code
    wrote its JSONL — every share-sheet spawn, until the user types — looks exactly
    like a sid that never existed. Answering ValueError there sent the app 4404
    where 4411 was the truth, and 4404 leaves the window parked on a Reconnect
    banner instead of dismissing it: the window outlives the session it was
    showing. What an attaching client asked is "does my session still stand",
    and the answer to that does not depend on what is on disk.
    """
    monkeypatch.setattr(jpty.managed, "is_open", lambda sid: False)
    monkeypatch.setattr("jstack_host.transcripts._find_session_cwd",
                        lambda sid: None)
    monkeypatch.setattr("jstack_host.board._live_session_ids",
                        lambda: set())
    with pytest.raises(jpty.SessionEnded):
        jpty._ensure_managed("abc12345")


def test_no_query_intent_can_ask_the_socket_to_revive(monkeypatch):
    """The revive is gone from the signature, not merely defaulted off.

    A knob that still exists is one a client can still turn — and the client
    that turned this one did not know it was asking. `_ensure_managed` takes
    the sid and nothing else, so no query string reaches a spawn.
    """
    import inspect
    assert list(inspect.signature(jpty._ensure_managed).parameters) == ["sid"]
    # ...and the endpoint no longer reads the intent it used to select on.
    assert "reattach" not in inspect.signature(jpty.pty_ws).parameters


def test_attach_to_mac_held_session_is_still_a_refusal(monkeypatch):
    monkeypatch.setattr(jpty.managed, "is_open", lambda sid: False)
    monkeypatch.setattr("jstack_host.transcripts._find_session_cwd",
                        lambda sid: "/tmp/somewhere")
    monkeypatch.setattr("jstack_host.board._live_session_ids",
                        lambda: {"abc12345"})
    spawned = _forbid_spawn(monkeypatch)
    with pytest.raises(RuntimeError):
        jpty._ensure_managed("abc12345")
    assert spawned == []


# ── the outbound pump — data, control frames, ordered closes ───────────────
#
# The pump used to carry only PTY bytes. Now it also carries JSON control
# frames (the open-thread command to a driving device) and ordered closes
# (close-on-other-instances ends a view with 4412, not an EOF guess), so the
# drain returns the ordered disposition — None keeps the EOF path.

class _FakeWS:
    def __init__(self):
        self.sent = []

    async def send_bytes(self, data):
        self.sent.append(("bytes", data))

    async def send_text(self, text):
        self.sent.append(("text", text))


def test_drain_flushes_output_then_returns_ordered_close():
    import asyncio

    async def main():
        ws, q = _FakeWS(), asyncio.Queue()
        q.put_nowait(("data", b"hello"))
        q.put_nowait(("text", '{"type":"open","url":"jremote://session/x"}'))
        q.put_nowait(("close", 4412, "dismissed from another device"))
        return ws, await jpty._drain(ws, q)

    ws, disposition = asyncio.run(main())
    assert disposition == (4412, "dismissed from another device")
    assert ws.sent == [
        ("bytes", b"hello"),
        ("text", '{"type":"open","url":"jremote://session/x"}'),
    ]


def test_drain_eof_returns_none_for_the_eof_disposition_path():
    import asyncio

    async def main():
        ws, q = _FakeWS(), asyncio.Queue()
        q.put_nowait(("data", b"tail"))
        q.put_nowait(None)
        return ws, await jpty._drain(ws, q)

    ws, disposition = asyncio.run(main())
    assert disposition is None
    assert ws.sent == [("bytes", b"tail")]


def test_attach_advertises_synchronized_output():
    """The CLI brackets each repaint in DEC 2026 and the app's terminal honours
    it, but tmux only wraps its own repaints for a client that advertises the
    feature. Drop the flag and the app paints half-applied frames — the caret
    strobes and lands over content instead of on the prompt."""
    argv = jpty._attach_argv("abc12345")
    assert "-T" in argv
    assert argv[argv.index("-T") + 1] == "sync"


def test_client_features_precede_the_attach_command():
    """`-T` is a tmux global flag. Placed after `attach` it is an unknown
    option to the command and the client never comes up at all."""
    argv = jpty._attach_argv("abc12345")
    assert argv.index("-T") < argv.index("attach")


# ── which machine is behind the socket ─────────────────────────────────────

class _Peer:
    def __init__(self, host):
        self.client = type("C", (), {"host": host})() if host is not None else None


@pytest.mark.parametrize("addr", ["127.0.0.1", "::1", "::ffff:127.0.0.1"])
def test_loopback_client_is_the_desk(addr):
    """The app on the host's own machine talks to it over loopback."""
    assert jpty._on_desk(_Peer(addr)) is True


@pytest.mark.parametrize("addr", ["10.66.0.9", "192.168.0.190", "10.66.0.12"])
def test_any_other_address_is_not_the_desk(addr):
    """The work Mac over the mesh, a phone on the LAN. `platform=mac` cannot
    tell the first of those from the desk — the address can, and a spawn it
    drives belongs on its screen, not on the host's."""
    assert jpty._on_desk(_Peer(addr)) is False


def test_no_peer_address_reads_as_the_desk():
    """Unknown keeps today's behavior: a window somewhere beats none."""
    assert jpty._on_desk(_Peer(None)) is True
    assert jpty._on_desk(_Peer("")) is True
