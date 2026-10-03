"""A machine-spawned session must show WHY it exists.

A cron wake / review spawn injects its task as the first user message, wrapped
in a routing marker (`[cron:<id> <label>] …`, `[POST-SESSION-REVIEW] …`). The
old filters treated the whole message as noise, so the board card fell back to
"New session" and the thread showed only the agent's output — a headless
session the user could not attribute. Law: the marker is noise, the body is the
session's identity — strip the marker, surface the task, everywhere the
session presents itself. Genuinely machine-shaped noise (`<`-wrapped
reminders, hook output) stays dropped.
"""

import json

import pytest

import jstack_host.board as board
import jstack_host.messages as messages

CRON = ("[cron:75be326e-517d-4f1c-9e53-e9c2100fd73c Nova-chat wake "
        "2026-08-16 08:00 PT] Verify the board.py seam on its first batch.\n\n"
        "Measure, don't assume: re-run the breakdown.")
REVIEW = "[POST-SESSION-REVIEW] Review session abc123 for dropped threads."


# --- the seam itself ---------------------------------------------------------

def test_spawn_task_strips_cron_marker():
    body = messages.spawn_task(CRON)
    assert body.startswith("Verify the board.py seam")
    assert "[cron:" not in body


def test_spawn_task_strips_review_marker():
    assert messages.spawn_task(REVIEW) == (
        "Review session abc123 for dropped threads.")


def test_spawn_task_strips_the_booked_by_line():
    # Provenance for the woken session to read; the card shows the task.
    booked = CRON.replace("PT] ", "PT] [booked by session 1e668fad — transcript "
                          "/x/1e668fad.jsonl] That session scheduled this run.\n\n", 1)
    body = messages.spawn_task(booked)
    assert body.startswith("Verify the board.py seam"), body
    assert "booked by" not in body


def test_spawn_task_ignores_ordinary_text():
    assert messages.spawn_task("fix the login bug") == ""
    assert messages.spawn_task("<system-reminder>x</system-reminder>") == ""
    assert messages.spawn_task("[ISS-0481] not a spawn marker") == ""
    assert messages.spawn_task("") == ""


# --- board card preview ------------------------------------------------------

def test_preview_surfaces_cron_task_not_blank():
    got = board._preview({"first_real_user_msg": CRON, "first_msg": CRON})
    assert got.startswith("Verify the board.py seam")


def test_preview_still_prefers_titles():
    got = board._preview({"custom_title": "Board seam verdict",
                          "first_real_user_msg": CRON})
    assert got == "Board seam verdict"


def test_preview_still_drops_angle_noise():
    got = board._preview({"first_msg": "<local-command-stdout></local-command-stdout>",
                          "slug": "quiet-fox"})
    assert got == "Quiet Fox"


def test_convo_lines_surface_cron_prompt():
    prompt, _ = board._convo_lines({"last_prompt": CRON, "last_msg": "done"})
    assert prompt.startswith("Verify the board.py seam")


def test_convo_lines_still_drop_angle_noise():
    prompt, _ = board._convo_lines(
        {"last_prompt": "<task-notification>x</task-notification>",
         "last_msg": "ok"})
    assert prompt == ""


# --- the store's history rows ------------------------------------------------

def test_history_row_serves_the_injected_task():
    import jstack_host.store as store
    row = {"session_id": "x", "agent_id": "nova", "sub_mode": "chat",
           "first_real_user_msg": CRON, "first_msg": CRON, "last_prompt": CRON,
           "last_msg": "verdict logged"}
    served = store._serve_session(row)
    assert served["preview"].startswith("Verify the board.py seam")
    assert served["last_prompt"].startswith("Verify the board.py seam")
    assert served["last_reply"] == "verdict logged"


# --- the thread --------------------------------------------------------------

SID = "6fba7c2e-09d1-4751-99b0-2e47d872b0d2"


def _write_session(projects, lines):
    pd = projects / "-ws-chat"
    pd.mkdir(parents=True)
    f = pd / f"{SID}.jsonl"
    f.write_text("\n".join(json.dumps(x) for x in lines) + "\n")


def _user(text):
    return {"type": "user", "message": {"role": "user", "content": text},
            "timestamp": "2026-08-16T23:45:13Z"}


@pytest.fixture
def projects(tmp_path, monkeypatch):
    root = tmp_path / "projects"
    root.mkdir()
    monkeypatch.setattr(messages, "_CLAUDE_PROJECTS", root)
    return root


def test_thread_shows_injected_task(projects):
    _write_session(projects, [
        _user(CRON),
        {"type": "assistant", "message": {
            "role": "assistant",
            "content": [{"type": "text", "text": "On it."}]},
         "timestamp": "2026-08-16T23:45:20Z"},
    ])
    msgs = messages.parse_session(SID)["messages"]
    assert [m["role"] for m in msgs] == ["user", "assistant"]
    assert msgs[0]["text"].startswith("Verify the board.py seam")
    assert "[cron:" not in msgs[0]["text"]


def test_thread_still_drops_machine_noise(projects):
    _write_session(projects, [
        _user("<system-reminder>ignore me</system-reminder>"),
        _user("Caveat: the messages below were generated"),
        {"type": "assistant", "message": {
            "role": "assistant",
            "content": [{"type": "text", "text": "hello"}]},
         "timestamp": "2026-08-16T23:45:20Z"},
    ])
    msgs = messages.parse_session(SID)["messages"]
    assert [m["role"] for m in msgs] == ["assistant"]
