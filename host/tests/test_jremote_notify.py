"""Done-processing pushes: edges, mutes, and already-looking suppression.

The contract: a managed session's working dot going out — after a stretch long
enough to mean work — pushes to the phone, unless the user is already in that
thread (PTY attached / app foregrounded on it) or muted the agent. Everything
else on the board (raw windows, headless workers, quick conversational
replies, sessions that vanish) stays silent.
"""

import asyncio
import os
import json
import time

import pytest

from jstack_host import board_watch, events, notify, notify_watch


@pytest.fixture(autouse=True)
def events_file(monkeypatch, tmp_path):
    """Every test writes the event log to scratch — never the live state.
    Pruning is opt-in (its own test); the default skips it."""
    monkeypatch.setattr(events, "_FILE", tmp_path / "events.jsonl")
    monkeypatch.setattr(events, "_pruned", True)
    return events


# ── helpers ──

def _row(sid="s1", *, managed=True, live=False, agent="nova", name="Nova",
         reply="all done", turn=None, last_activity="t0", path="",
         prompt="what did you ship"):
    """A board row. `turn` mirrors `live` unless set apart — the engine keys on
    turn (sharp) and only falls back to live (sticky) when it reads ""."""
    return {"session_id": sid, "managed": managed, "live": live,
            "turn": (("working" if live else "idle") if turn is None else turn),
            "agent_id": agent, "agent_name": name, "emoji": "⚡",
            "sub_mode": "chat", "last_reply": reply, "preview": "topic",
            "last_activity": last_activity, "last_prompt": prompt, "path": path}


@pytest.fixture
def clock(monkeypatch):
    """A controllable clock for notify_watch's edge timing."""
    state = {"now": 1000.0}

    class _Time:
        @staticmethod
        def time():
            return state["now"]

    monkeypatch.setattr(notify_watch, "time", _Time)

    def advance(sec):
        state["now"] += sec

    return advance


class _Fired(list):
    """Captured pushes, plus the fake unread set the watcher consults."""
    unread: set


@pytest.fixture
def fired(monkeypatch):
    """Capture pushes instead of sending them; clean edge state per test.
    The watcher's unread reads are faked too, so no test touches live state."""
    calls = _Fired()
    calls.unread = set()

    def fake_notify(sid, agent_id, *, title, body):
        calls.append({"sid": sid, "agent": agent_id, "title": title, "body": body})
        calls.unread.add(sid)
        return True

    monkeypatch.setattr(notify_watch.notify, "notify", fake_notify)
    monkeypatch.setattr(notify_watch.notify, "unread_sids", lambda: set(calls.unread))
    monkeypatch.setattr(notify_watch, "_state", {})
    monkeypatch.setattr(notify_watch, "_marked", {})
    monkeypatch.setattr(notify_watch, "_progress", {})
    monkeypatch.setattr(notify_watch, "_fired_done", {})
    return calls


@pytest.fixture
def progressed(monkeypatch):
    """Captured progress relays — separate from done-pushes on purpose."""
    calls = []

    def fake_progress(sid, agent_id, *, title, body):
        calls.append({"sid": sid, "agent": agent_id, "title": title,
                      "body": body})
        return True

    monkeypatch.setattr(notify_watch.notify, "progress", fake_progress)
    return calls


class _InlineThread:
    """Runs the notify send synchronously so tests see it land."""

    def __init__(self, target=None, kwargs=None, daemon=None):
        self._target, self._kwargs = target, kwargs or {}

    def start(self):
        self._target(**self._kwargs)


@pytest.fixture
def store(monkeypatch, tmp_path):
    """notify's decision layer against a scratch state file, APNs faked out."""
    import types
    monkeypatch.setattr(notify, "_STATE", tmp_path / "devices.json")
    monkeypatch.setattr(notify, "_attached", {})
    monkeypatch.setattr(notify, "threading",
                        types.SimpleNamespace(Thread=_InlineThread,
                                              Lock=notify.threading.Lock))
    sent = []

    def fake_send(token, *, title, body, badge=None, session_id="",
                  collapse_id=""):
        sent.append({"token": token, "title": title, "badge": badge,
                     "session_id": session_id, "collapse_id": collapse_id})
        return True, "sent"

    monkeypatch.setattr(notify.apns, "send", fake_send)
    monkeypatch.setattr(notify.apns, "is_configured", lambda: True)
    return sent


# ── notify_watch: the edge ──

def test_first_observation_seeds_without_firing(clock, fired):
    """A dashboard restart into a busy Mac must stay silent — a state is not
    an edge."""
    notify_watch.observe([_row(live=True)])
    clock(60)
    notify_watch.observe([_row(live=True)])
    assert fired == []


def test_working_to_idle_after_real_work_fires(clock, fired):
    notify_watch.observe([_row(live=False)])
    notify_watch.observe([_row(live=True)])
    clock(30)
    notify_watch.observe([_row(live=False)])
    assert len(fired) == 1
    assert fired[0]["sid"] == "s1"
    assert "Nova" in fired[0]["title"]
    assert fired[0]["body"] == "all done"


def test_a_stop_that_is_a_wait_says_so(clock, fired):
    """A turn that closed on a permission prompt must not push the previous
    reply as if it were done — the tap is wanted for a different reason."""
    notify_watch.observe([_row(live=False)])
    notify_watch.observe([_row(live=True)])
    clock(30)
    row = _row(live=False)
    row["attention"] = "waiting"
    notify_watch.observe([row])
    assert len(fired) == 1
    assert fired[0]["body"] == "Waiting on your OK to continue."


def test_a_quick_reply_is_conversation_not_work(clock, fired):
    """Under MIN_WORKING the ping would land while the user is mid-conversation."""
    notify_watch.observe([_row(live=False)])
    notify_watch.observe([_row(live=True)])
    clock(notify_watch.MIN_WORKING / 2)
    notify_watch.observe([_row(live=False)])
    assert fired == []


def test_unmanaged_rows_never_fire(clock, fired):
    """Raw iTerm windows and headless workers are not phone business."""
    for managed in (False,):
        notify_watch.observe([_row(managed=managed, live=False)])
        notify_watch.observe([_row(managed=managed, live=True)])
        clock(60)
        notify_watch.observe([_row(managed=managed, live=False)])
    assert fired == []


def test_a_vanished_session_is_forgotten_not_finished(clock, fired):
    """A close mid-work is an ending, not a completion — and its state must
    not leak so a reused sid seeds fresh."""
    notify_watch.observe([_row(live=True)])
    clock(60)
    notify_watch.observe([])
    assert fired == []
    assert notify_watch._state == {}


def test_turn_outranks_sticky_live(clock, fired):
    """`live` lingers ~90s past the last write; a row whose turn has closed
    must read done even while live still says working — otherwise every ping
    lands a minute and a half late."""
    notify_watch.observe([_row(live=True, turn="working")])
    clock(30)
    notify_watch.observe([_row(live=True, turn="idle")])
    assert len(fired) == 1


def test_missing_turn_falls_back_to_live(clock, fired):
    row = {k: v for k, v in _row(live=True).items() if k != "turn"}
    idle = {k: v for k, v in _row(live=False).items() if k != "turn"}
    notify_watch.observe([row])
    clock(30)
    notify_watch.observe([idle])
    assert len(fired) == 1


def test_an_unread_turn_clock_falls_back_to_live(clock, fired):
    """"" is not idle — it is "nobody looked". A row whose clock was never
    read (a Codex pane, a managed session with no transcript yet) must be
    judged on the only evidence there is, or its first pass reads as a turn
    that just ended and pushes a done for a reply nobody saw land."""
    notify_watch.observe([_row(live=True, turn="")])
    clock(30)
    notify_watch.observe([_row(live=True, turn="")])
    assert fired == [], "a blank clock read as an idle turn"
    notify_watch.observe([_row(live=False, turn="")])
    assert len(fired) == 1, "the fallback stopped answering once live dropped"


def test_two_cycles_fire_twice(clock, fired):
    notify_watch.observe([_row(live=False)])
    for _ in range(2):
        notify_watch.observe([_row(live=True)])
        clock(30)
        notify_watch.observe([_row(live=False)])
    assert len(fired) == 2


# ── notify: the decision ──

def test_push_reaches_every_registered_device(store):
    notify.register("tok-a")
    notify.register("tok-b")
    assert notify.notify("s1", "nova", title="t", body="b") is True
    assert {s["token"] for s in store} == {"tok-a", "tok-b"}
    assert all(s["session_id"] == "s1" for s in store)


def test_muted_agent_is_silent_across_seats(store):
    """Muting keys on the base agent: 'nova-chat' from the roster mutes
    board rows carrying 'nova'."""
    notify.register("tok")
    notify.set_muted("nova-chat", True)
    assert notify.notify("s1", "nova", title="t", body="b") is False
    notify.set_muted("nova", False)
    assert notify.notify("s1", "nova", title="t", body="b") is True


def test_pty_attached_thread_is_silent(store):
    """The user is literally looking at the terminal — no push."""
    notify.register("tok")
    notify.mark_attached("s1")
    assert notify.notify("s1", "nova", title="t", body="b") is False
    notify.unmark_attached("s1")
    assert notify.notify("s1", "nova", title="t", body="b") is True


def test_reconnect_overlap_keeps_the_mark(store):
    """New PTY attach overlaps the old one's teardown: the old unmark must
    not erase the new mark."""
    notify.register("tok")
    notify.mark_attached("s1")     # old connection
    notify.mark_attached("s1")     # reconnect
    notify.unmark_attached("s1")   # old teardown
    assert notify.notify("s1", "nova", title="t", body="b") is False


def test_foregrounded_thread_is_silent_until_stale(store, monkeypatch):
    notify.register("tok")
    notify.set_foreground("s1")
    assert notify.notify("s1", "nova", title="t", body="b") is False
    # other threads still push
    assert notify.notify("s2", "nova", title="t", body="b") is True
    # a stale heartbeat means the app backgrounded
    d = notify._load()
    d["fg_at"] = time.time() - notify._FG_TTL - 1
    notify._save(d)
    assert notify.notify("s1", "nova", title="t", body="b") is True


def test_no_devices_means_no_push(store):
    assert notify.notify("s1", "nova", title="t", body="b") is False


def test_unread_marks_finished_sessions_and_badge_is_their_count(store):
    notify.register("tok")
    notify.notify("s1", "nova", title="t", body="b")
    notify.notify("s2", "nova", title="t", body="b")
    assert notify.unread_sids() == {"s1", "s2"}
    assert store[-1]["badge"] == 2


def test_opening_the_thread_clears_only_that_session(store):
    notify.register("tok")
    notify.notify("s1", "nova", title="t", body="b")
    notify.notify("s2", "nova", title="t", body="b")
    notify.set_foreground("s1")
    assert notify.unread_sids() == {"s2"}
    notify.set_foreground(None)          # backgrounding clears nothing
    assert notify.unread_sids() == {"s2"}


def test_pty_attach_is_interaction(store):
    notify.register("tok")
    notify.notify("s1", "nova", title="t", body="b")
    notify.mark_attached("s1")
    assert notify.unread_sids() == set()


def test_muted_agent_gets_the_dot_but_no_buzz(store):
    """Mute means don't buzz me — the quiet unread state still shows."""
    notify.register("tok")
    notify.set_muted("nova", True)
    assert notify.notify("s1", "nova", title="t", body="b") is False
    assert notify.unread_sids() == {"s1"}
    assert store == []


def test_already_looking_means_never_unread(store):
    notify.register("tok")
    notify.set_foreground("s1")
    assert notify.notify("s1", "nova", title="t", body="b") is False
    assert notify.unread_sids() == set()


def test_dead_device_tokens_are_pruned(store, monkeypatch):
    """Every reason APNs will never take the token again removes it:
    BadDeviceToken (wrong env, both tried), Unregistered (install gone),
    BadEnvironmentKeyInToken (a simulator token reached real APNs). Left in
    place each draws an error on every send, forever."""
    fates = {"tok-env": "apns 400: BadDeviceToken",
             "tok-gone": 'apns 410: {"reason":"Unregistered"}',
             "tok-sim": 'apns 403: {"reason":"BadEnvironmentKeyInToken"}'}
    for t in [*fates, "tok-live"]:
        notify.register(t)

    def fake_send(token, **kw):
        if token in fates:
            return False, fates[token]
        return True, "sent"

    monkeypatch.setattr(notify.apns, "send", fake_send)
    notify._send_all([*fates, "tok-live"], title="t", body="b",
                     badge=1, session_id="s1")
    assert notify._load()["tokens"] == ["tok-live"]


@pytest.fixture
def cleared(monkeypatch):
    out = []
    monkeypatch.setattr(notify_watch.notify, "clear_unread", out.append)
    return out


def test_a_turn_opening_clears_unread(clock, fired, cleared):
    """Typing into the session — from the Mac or the phone — is interaction."""
    notify_watch.observe([_row(live=False)])
    notify_watch.observe([_row(live=True)])
    assert cleared == ["s1"]


def test_a_quick_exchange_still_clears_unread(clock, fired, cleared):
    """A sub-tick turn (typed and replied between observations) never shows a
    turn_open edge — the new prompt sitting in the transcript is the
    interaction fact, readable after the fact."""
    notify_watch.observe([_row(live=False, prompt="p1")])
    notify_watch.observe([_row(live=True, prompt="p1")])
    clock(30)
    notify_watch.observe([_row(live=False, prompt="p1")])   # fires, marks p1
    assert len(fired) == 1
    before = len(cleared)
    notify_watch.observe([_row(live=False, prompt="p2")])   # they typed again
    assert cleared[before:] == ["s1"]


def test_a_transcript_write_with_no_prompt_keeps_the_mark(clock, fired, cleared):
    """The bug this engine was losing answers to: an idle session's JSONL is
    rewritten with no new content — no prompt, no turn, nothing typed — and
    the mark used to die with the mtime, greying out a session the user had never
    opened. Only a prompt is evidence they looked."""
    notify_watch.observe([_row(live=True, prompt="p1")])
    clock(30)
    notify_watch.observe([_row(live=False, prompt="p1")])   # fires, marks p1
    assert len(fired) == 1
    before = len(cleared)
    # Same prompt, transcript touched — a dozen ticks of it.
    for stamp in ("t3", "t4", "t5"):
        notify_watch.observe([_row(live=False, prompt="p1", last_activity=stamp)])
    assert cleared[before:] == []


def test_an_unreadable_prompt_keeps_the_mark(clock, fired, cleared):
    """A blank prompt is the board failing to read the transcript this tick,
    not the user typing. Clearing on it would drop the dot on a bad pass."""
    notify_watch.observe([_row(live=True, prompt="p1")])
    clock(30)
    notify_watch.observe([_row(live=False, prompt="p1")])   # fires, marks p1
    before = len(cleared)
    notify_watch.observe([_row(live=False, prompt="")])
    assert cleared[before:] == []


def test_a_vanished_session_clears_unread(clock, fired, cleared):
    """Closed on either end: no session, no dot, no badge share."""
    notify_watch.observe([_row(live=False)])
    notify_watch.observe([])
    assert cleared == ["s1"]


def test_restart_reseeds_marks_so_a_later_prompt_still_clears(clock, fired, cleared):
    """_marked is in-memory; a persisted unread mark must survive a dashboard
    restart with its clearing intact — the first observation adopts the row's
    prompt, and the next one clears."""
    fired.unread.add("s1")   # marked before "the restart"; _marked is empty
    notify_watch.observe([_row(live=False, prompt="p5")])
    before = len(cleared)
    notify_watch.observe([_row(live=False, prompt="p6")])
    assert cleared[before:] == ["s1"]


# ── notify_watch: the progress relay ──

def test_progress_relays_fresh_narration_after_min_turn(clock, fired, progressed):
    """A long turn's interstitial narration reaches the phone as a 'working'
    update — the model already wrote the line, the relay just forwards it."""
    notify_watch.observe([_row(live=False, reply="old answer")])
    notify_watch.observe([_row(live=True, reply="old answer")])
    clock(notify_watch.PROGRESS_MIN_TURN + 1)
    notify_watch.observe([_row(live=True, reply="reading the board code")])
    assert len(progressed) == 1
    assert progressed[0]["body"] == "reading the board code"
    assert progressed[0]["title"].endswith("· working")
    assert fired == []          # the turn is still open — no done-push


def test_progress_never_relays_the_previous_turns_reply(clock, fired, progressed):
    """The reply text a turn opens on is the LAST turn's answer."""
    notify_watch.observe([_row(live=False, reply="old answer")])
    notify_watch.observe([_row(live=True, reply="old answer")])
    clock(notify_watch.PROGRESS_MIN_TURN + 1)
    notify_watch.observe([_row(live=True, reply="old answer")])
    assert progressed == []


def test_a_reply_ending_in_space_is_still_the_previous_turns_reply(
        clock, fired, progressed):
    """The board hands over a reply cut at 120 characters, so roughly one in
    six ends mid-space. Comparing the relay's stripped text against an
    unstripped baseline made those replies unequal to themselves, and the
    answer the user had just been pushed came straight back as this turn's
    progress — the whole of the identical-body traffic in the events log."""
    notify_watch.observe([_row(live=False, reply="old answer ")])
    notify_watch.observe([_row(live=True, reply="old answer ")])
    clock(notify_watch.PROGRESS_MIN_TURN + 1)
    notify_watch.observe([_row(live=True, reply="old answer ")])
    assert progressed == []


def test_progress_young_turn_stays_silent(clock, fired, progressed):
    notify_watch.observe([_row(live=False)])
    notify_watch.observe([_row(live=True)])
    clock(notify_watch.PROGRESS_MIN_TURN / 2)
    notify_watch.observe([_row(live=True, reply="early narration")])
    assert progressed == []


def test_progress_respects_cooldown(clock, fired, progressed):
    notify_watch.observe([_row(live=False, reply="old")])
    notify_watch.observe([_row(live=True, reply="old")])
    clock(notify_watch.PROGRESS_MIN_TURN + 1)
    notify_watch.observe([_row(live=True, reply="step one")])
    clock(10)
    notify_watch.observe([_row(live=True, reply="step two")])
    assert len(progressed) == 1
    clock(notify_watch.PROGRESS_COOLDOWN)
    notify_watch.observe([_row(live=True, reply="step three")])
    assert len(progressed) == 2
    assert progressed[-1]["body"] == "step three"


def test_progress_state_dies_with_the_turn(clock, fired, progressed):
    """Turn closes → done-push fires, relay state drops; the next turn
    re-baselines and does not replay the finished turn's narration."""
    notify_watch.observe([_row(live=False, reply="old")])
    notify_watch.observe([_row(live=True, reply="old")])
    clock(notify_watch.PROGRESS_MIN_TURN + 1)
    notify_watch.observe([_row(live=True, reply="step one")])
    notify_watch.observe([_row(live=False, reply="final answer")])
    assert len(fired) == 1
    assert notify_watch._progress == {}
    notify_watch.observe([_row(live=True, reply="final answer")])
    clock(notify_watch.PROGRESS_MIN_TURN + 1)
    notify_watch.observe([_row(live=True, reply="final answer")])
    assert len(progressed) == 1  # nothing new relayed


def test_progress_restart_mid_turn_adopts_baseline(clock, fired, progressed):
    """A dashboard restart into a running turn must not replay the narration
    it lands on — only text that changes after the restart relays."""
    notify_watch.observe([_row(live=True, reply="mid text")])
    clock(notify_watch.PROGRESS_MIN_TURN + 1)
    notify_watch.observe([_row(live=True, reply="mid text")])
    assert progressed == []
    notify_watch.observe([_row(live=True, reply="fresh step")])
    assert len(progressed) == 1


def test_unmanaged_rows_never_relay_progress(clock, fired, progressed):
    notify_watch.observe([_row(managed=False, live=True)])
    clock(notify_watch.PROGRESS_MIN_TURN + 1)
    notify_watch.observe([_row(managed=False, live=True, reply="narr")])
    assert progressed == []


# ── notify.progress: the decision ──

def test_progress_pushes_without_unread_or_badge(store):
    notify.register("tok")
    assert notify.progress("s1", "nova", title="t", body="b") is True
    assert notify.unread_sids() == set()
    assert store[-1]["badge"] is None
    assert store[-1]["collapse_id"] == "prog-s1"


def test_progress_toggle_mutes_only_progress(store):
    notify.register("tok")
    notify.set_progress_muted("nova-chat", True)
    assert notify.progress("s1", "nova", title="t", body="b") is False
    assert notify.unread_sids() == set()      # no quiet dot either — an FYI
    assert notify.notify("s1", "nova", title="t", body="b") is True
    notify.set_progress_muted("nova", False)
    assert notify.progress("s2", "nova", title="t", body="b") is True


def test_full_mute_silences_progress_too(store):
    notify.register("tok")
    notify.set_muted("nova", True)
    assert notify.progress("s1", "nova", title="t", body="b") is False


def test_progress_suppressed_while_the_user_is_looking(store):
    notify.register("tok")
    notify.mark_attached("s1")
    assert notify.progress("s1", "nova", title="t", body="b") is False
    notify.unmark_attached("s1")
    notify.set_foreground("s2")
    assert notify.progress("s2", "nova", title="t", body="b") is False
    assert notify.progress("s1", "nova", title="t", body="b") is True


def test_prefs_endpoint_routes_scopes(store):
    from jstack_host import router as r
    out = r.notify_set_prefs(r.MuteBody(agent_id="nova", muted=True,
                                        scope="progress"))
    assert out == {"muted_agents": [], "progress_muted_agents": ["nova"]}
    out = r.notify_set_prefs(r.MuteBody(agent_id="nova", muted=True))
    assert out["muted_agents"] == ["nova"]
    assert r.notify_get_prefs() == out


# ── events: the timeline record behind the pushes ──

def test_done_edge_records_an_event(clock, fired):
    notify_watch.observe([_row(live=False)])
    notify_watch.observe([_row(live=True)])
    clock(30)
    notify_watch.observe([_row(live=False)])
    log = events.since("")
    assert len(log) == 1
    assert log[0]["kind"] == "done"
    assert log[0]["session_id"] == "s1"
    assert log[0]["body"] == "all done"
    assert log[0]["pushed"] is True


def test_waiting_and_error_edges_record_their_kind(clock, fired):
    for attention in ("waiting", "error"):
        notify_watch.observe([_row(sid=attention, live=False)])
        notify_watch.observe([_row(sid=attention, live=True)])
        clock(30)
        row = _row(sid=attention, live=False)
        row["attention"] = attention
        notify_watch.observe([row])
    kinds = {e["session_id"]: e["kind"] for e in events.since("")}
    assert kinds == {"waiting": "waiting", "error": "error"}


def test_progress_relay_records_an_event(clock, fired, progressed):
    notify_watch.observe([_row(live=False, reply="old")])
    notify_watch.observe([_row(live=True, reply="old")])
    clock(notify_watch.PROGRESS_MIN_TURN + 1)
    notify_watch.observe([_row(live=True, reply="reading the board")])
    log = events.since("")
    assert len(log) == 1
    assert log[0]["kind"] == "progress"
    assert log[0]["body"] == "reading the board"


def test_suppressed_push_still_records(clock, fired, monkeypatch):
    """The whole point of recording at the source: The user already looking
    silences the buzz, never the timeline."""
    monkeypatch.setattr(notify_watch.notify, "notify",
                        lambda *a, **kw: False)
    notify_watch.observe([_row(live=False)])
    notify_watch.observe([_row(live=True)])
    clock(30)
    notify_watch.observe([_row(live=False)])
    log = events.since("")
    assert len(log) == 1
    assert log[0]["pushed"] is False


def test_since_cursor_is_exclusive():
    events.record("s1", "nova", "done", "t", "one", True)
    events.record("s1", "nova", "progress", "t", "two", False)
    log = events.since("")
    assert [e["body"] for e in log] == ["one", "two"]
    newer = events.since(log[0]["ts"])
    assert [e["body"] for e in newer] == ["two"]
    assert events.since(log[-1]["ts"]) == []


def test_prune_drops_only_expired_entries(monkeypatch):
    events.record("s1", "nova", "done", "t", "old", True)
    events.record("s1", "nova", "done", "t", "fresh", True)
    log = events.since("")
    # Backdate the first entry past retention, straight in the file.
    old = dict(log[0], ts="2000-01-01T00:00:00.000")
    events._FILE.write_text(
        json.dumps(old) + "\n" + json.dumps(log[1]) + "\n")
    monkeypatch.setattr(events, "_pruned", False)
    events.record("s1", "nova", "done", "t", "newest", True)
    assert [e["body"] for e in events.since("")] == ["fresh", "newest"]


def test_events_endpoint_serves_the_cursor():
    from jstack_host import router as r
    events.record("s1", "nova", "done", "t", "one", True)
    out = r.notify_events()
    assert [e["body"] for e in out["events"]] == ["one"]
    assert r.notify_events(since=out["events"][0]["ts"]) == {"events": []}


# ── events.timeline: derived from the transcript, log overlaid ──

SID = "cafebabe-0000-4000-8000-00000000c0de"


def _z(epoch):
    from datetime import datetime, timezone
    return datetime.fromtimestamp(epoch, timezone.utc).isoformat(
        timespec="milliseconds").replace("+00:00", "Z")


def _u(epoch, text):
    return {"type": "user", "timestamp": _z(epoch),
            "message": {"role": "user",
                        "content": [{"type": "text", "text": text}]}}


def _a(epoch, text):
    return {"type": "assistant", "timestamp": _z(epoch),
            "message": {"role": "assistant",
                        "content": [{"type": "text", "text": text}]}}


@pytest.fixture
def transcript(monkeypatch, tmp_path):
    """Write a synthetic session JSONL that timeline() derives from.
    Returns a writer: (entries, mtime_age) -> sid. Old mtime by default —
    a transcript nobody is writing to."""
    import os
    from jstack_host import messages
    proj = tmp_path / "proj"
    proj.mkdir()
    monkeypatch.setattr(messages, "_CLAUDE_PROJECTS", tmp_path)

    def write(entries, mtime_age=3600.0):
        path = proj / f"{SID}.jsonl"
        with path.open("w") as f:
            for e in entries:
                f.write(json.dumps(e) + "\n")
        stamp = time.time() - mtime_age
        os.utime(path, (stamp, stamp))
        return SID

    return write


def test_timeline_derives_turns_at_push_cadence(transcript):
    t0 = 1_700_000_000
    sid = transcript([
        _u(t0, "build the thing"),
        _a(t0 + 30, "too early"),           # before PROGRESS_MIN_TURN
        _a(t0 + 70, "step one"),
        _a(t0 + 100, "inside cooldown"),    # < PROGRESS_COOLDOWN after step one
        _a(t0 + 70 + 190, "step three"),
        _a(t0 + 300, "all done"),
    ])
    got = events.timeline(sid)
    assert [(e["kind"], e["body"]) for e in got] == [
        ("progress", "step one"),
        ("progress", "step three"),
        ("done", "all done"),
    ]


def test_timeline_quick_turn_is_conversation(transcript):
    t0 = 1_700_000_000
    sid = transcript([_u(t0, "hey"), _a(t0 + 3, "hey the user")])
    assert events.timeline(sid) == []


def test_timeline_open_turn_ends_in_progress_not_done(transcript):
    """A transcript still being written: the newest narration is happening
    now — calling it done would lie for every live session."""
    t0 = time.time() - 120
    sid = transcript([_u(t0, "go"), _a(t0 + 90, "reading the board")],
                     mtime_age=5.0)
    got = events.timeline(sid)
    assert [e["kind"] for e in got] == ["progress"]
    assert got[0]["body"] == "reading the board"


def test_timeline_overlay_lands_pushed_and_inserts_waiting(transcript):
    t0 = 1_700_000_000
    sid = transcript([_u(t0, "go"), _a(t0 + 60, "all done")])
    events.record(sid, "nova", "done", "t", "all done", True)
    events.record(sid, "nova", "waiting", "t",
                  "Waiting on your OK to continue.", True)
    events.record(sid, "nova", "progress", "t", "never derived", False)
    got = events.timeline(sid)
    kinds = [(e["kind"], e["pushed"]) for e in got]
    assert ("done", True) in kinds          # pushed flag landed on the twin
    assert ("waiting", True) in kinds       # transcript-invisible → inserted
    assert all(e["body"] != "never derived" for e in got)  # unmatched → dropped


def test_timeline_carries_each_trigger_fire_with_its_outcome(transcript):
    """A fire of the session's environment lands in its timeline, which is the jRemote
    sidebar: trigger name first (the app shows the body, not the title), outcome last."""
    from jstack_host import triggers
    t0 = 1_700_000_000
    sid = transcript([_u(t0, "go"), _a(t0 + 60, "all done")])
    triggers.record(sid=sid, engine="claude", event="Stop", trigger="compact-on-delivery",
                    met=True, fired=True, action="input", keys="/compact", fire="f1",
                    outcome="pending")
    triggers.settle("f1", "sent/continued")
    triggers.record(sid=sid, engine="claude", event="Stop", trigger="compact-on-delivery",
                    met=False, fired=False, reason="no reading")
    triggers.record(sid="11111111-other", trigger="x", met=True, fired=True, fire="f2")
    got = [e for e in events.timeline(sid) if e["kind"] == "trigger"]
    assert [e["body"] for e in got] == ["compact-on-delivery · /compact → sent/continued"]
    assert got[0]["session_id"] == sid and got[0]["title"] == "compact-on-delivery"
    assert "." in got[0]["ts"]  # the same naive local ISO the derived entries sort by


def test_timeline_missing_session_is_empty(transcript):
    assert events.timeline("00000000-0000-4000-8000-000000000000") == []


# ── board_watch: consumers keep the watcher alive ──

@pytest.fixture
def fast(monkeypatch):
    monkeypatch.setattr(board_watch, "TICK", 0.05)
    monkeypatch.setattr(board_watch, "TICK_IDLE", 0.05)
    monkeypatch.setattr(board_watch, "_subscribers", set())
    monkeypatch.setattr(board_watch, "_consumers", [])
    monkeypatch.setattr(board_watch, "_watcher", None)
    monkeypatch.setattr(board_watch, "_wake", None)
    monkeypatch.setattr(board_watch, "_loop", None)
    monkeypatch.setattr(board_watch, "_latest", None)
    monkeypatch.setattr(board_watch, "_digest", None)


def test_consumer_runs_with_no_subscribers(fast, monkeypatch):
    """The whole point: edges are observed while no phone is streaming."""
    state = {"rows": [_row(live=True)]}
    monkeypatch.setattr(board_watch, "_snapshot", lambda: state["rows"])
    seen = []

    async def main():
        board_watch.add_consumer(lambda rows: seen.append(rows))
        await board_watch.ensure_running()
        await asyncio.sleep(0.15)
        state["rows"] = [_row(live=False)]
        await asyncio.sleep(0.2)
        board_watch._consumers.clear()   # let the watcher wind down

    asyncio.run(main())
    assert any(r[0]["live"] for r in seen)
    assert any(not r[0]["live"] for r in seen)


def test_a_broken_consumer_does_not_take_the_board_down(fast, monkeypatch):
    state = {"rows": [_row(live=True)]}
    monkeypatch.setattr(board_watch, "_snapshot", lambda: state["rows"])
    seen = []

    def bad(rows):
        raise RuntimeError("consumer blew up")

    async def main():
        board_watch.add_consumer(bad)
        board_watch.add_consumer(lambda rows: seen.append(rows))
        await board_watch.ensure_running()
        await asyncio.sleep(0.1)
        state["rows"] = [_row(live=False)]
        await asyncio.sleep(0.2)
        board_watch._consumers.clear()

    asyncio.run(main())
    assert len(seen) >= 2, "watcher died with the broken consumer"


# ── the false-done audit ──
#
# A done edge cannot be judged where it fires: Stop clears the turn marker
# before the board can read idle, and the turn's closing message is the
# transcript's freshest write, so a real done and a false one look the same
# there. The verdict is taken at the next turn opening, which is where they
# differ — a real done is followed by a prompt, a false one by the same turn
# resuming. These tests hold that distinction.

def _turn_dir(monkeypatch, tmp_path):
    """Point the audit at a scratch turn-marker dir."""
    from jstack_host import board
    d = tmp_path / "turn"
    d.mkdir()
    monkeypatch.setattr(board, "_TURN_DIR", d)
    return d


def _done_then_reopen(clock, gap=5, before=None):
    """Work → done edge (at t=1030) → `gap`s later the turn is open again.
    `before` runs in between, to stage the marker the audit will read."""
    notify_watch.observe([_row(live=True)])
    clock(30)
    notify_watch.observe([_row(live=False)])
    if before:
        before()
    clock(gap)
    notify_watch.observe([_row(live=True, last_activity="t1")])


def test_a_turn_reopening_with_no_prompt_names_the_done_false(
        fired, clock, monkeypatch, tmp_path, capsys):
    """The signature the guard exists to catch: no prompt, so no new turn —
    the session never stopped, and the done that fired was invented."""
    _turn_dir(monkeypatch, tmp_path)
    _done_then_reopen(clock)
    assert len(fired) == 1, "the done edge itself still fires"
    assert "FALSE DONE [s1]" in capsys.readouterr().out


def test_a_prompt_after_the_done_leaves_it_alone(
        fired, clock, monkeypatch, tmp_path, capsys):
    """An ordinary turn boundary: The user typed, the marker was stamped, the
    reopening is a real new turn. The overwhelming majority of edges."""
    d = _turn_dir(monkeypatch, tmp_path)

    def prompt():
        marker = d / "s1"
        marker.write_text("open")
        os.utime(marker, (1040.0, 1040.0))   # stamped after the 1030 done

    _done_then_reopen(clock, before=prompt)
    assert "FALSE DONE" not in capsys.readouterr().out


def test_marker_residue_predating_the_done_cannot_vouch_for_it(
        fired, clock, monkeypatch, tmp_path, capsys):
    """A marker left behind by a session that died before its Stop is not a
    prompt. Existence alone would read it as one — hence the mtime."""
    d = _turn_dir(monkeypatch, tmp_path)

    def residue():
        marker = d / "s1"
        marker.write_text("open")
        os.utime(marker, (900.0, 900.0))     # older than the done itself

    _done_then_reopen(clock, before=residue)
    assert "FALSE DONE [s1]" in capsys.readouterr().out


def test_a_session_going_back_to_work_much_later_is_its_own_turn(
        fired, clock, monkeypatch, tmp_path, capsys):
    """Past the window the two cases stop being distinguishable, so the audit
    declines to rule rather than guess."""
    _turn_dir(monkeypatch, tmp_path)
    _done_then_reopen(clock, gap=notify_watch.FALSE_DONE_WINDOW + 1)
    assert "FALSE DONE" not in capsys.readouterr().out


def _tail(tmp_path, *records):
    """A transcript whose tail the audit will read to classify a reopening."""
    f = tmp_path / "s1.jsonl"
    f.write_text("\n".join(json.dumps(r) for r in records) + "\n")
    return str(f)


def _compacting(tmp_path):
    """What `/compact` leaves behind: the command echoed as a bare string,
    then the harness's own bookkeeping. No prompt was ever submitted."""
    return _tail(
        tmp_path,
        {"type": "assistant",
         "message": {"role": "assistant",
                     "content": [{"type": "text", "text": "all done"}]}},
        {"type": "user", "message": {"role": "user", "content": "/compact"}},
        {"type": "user", "isCompactSummary": True,
         "message": {"role": "user",
                     "content": "This session is being continued from a "
                                "previous conversation…"}},
        {"type": "user", "message": {"role": "user",
                                     "content": "<local-command-stdout>ok"
                                                "</local-command-stdout>"}})


def test_a_local_command_reopening_is_not_a_false_done(
        fired, clock, monkeypatch, tmp_path, capsys):
    """The first hit this audit ever logged, and it was wrong: The user answered a
    real done with `/compact`, which reopens the turn without submitting a
    prompt — the same unmarked reopening a resumed turn leaves. An unmarked
    reopening is only evidence when a prompt was what could have marked it."""
    _turn_dir(monkeypatch, tmp_path)
    path = _compacting(tmp_path)
    notify_watch.observe([_row(live=True, path=path)])
    clock(30)
    notify_watch.observe([_row(live=False, path=path)])
    clock(5)
    notify_watch.observe([_row(live=True, path=path, last_activity="t1")])
    assert "FALSE DONE" not in capsys.readouterr().out


def test_a_resumed_turn_with_a_transcript_still_reads_false(
        fired, clock, monkeypatch, tmp_path, capsys):
    """The bound on the exemption above — the check must still be able to go
    red. A reopening whose newest user line is a real prompt (blocks, not the
    bare string a typed command has) is judged exactly as before."""
    _turn_dir(monkeypatch, tmp_path)
    path = _tail(
        tmp_path,
        {"type": "assistant",
         "message": {"role": "assistant",
                     "content": [{"type": "text", "text": "all done"}]}},
        {"type": "user",
         "message": {"role": "user",
                     "content": [{"type": "text", "text": "/compact is nice"}]}})
    notify_watch.observe([_row(live=True, path=path)])
    clock(30)
    notify_watch.observe([_row(live=False, path=path)])
    clock(5)
    notify_watch.observe([_row(live=True, path=path, last_activity="t1")])
    assert "FALSE DONE [s1]" in capsys.readouterr().out


def test_a_closed_session_leaves_no_pending_verdict(fired, clock,
                                                    monkeypatch, tmp_path):
    """The row vanishing takes the held done with it — a sid reappearing
    later must not be judged against a done from a previous life."""
    _turn_dir(monkeypatch, tmp_path)
    notify_watch.observe([_row(live=True)])
    clock(30)
    notify_watch.observe([_row(live=False)])
    assert notify_watch._fired_done
    notify_watch.observe([])                 # session closed
    assert not notify_watch._fired_done
