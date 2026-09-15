"""The package's allowance reader — the Usage bars off Claude Code's own cache.

Pins the contract the app decodes: a provider never sampled is `null` (never a
zero meter), the CLI's cached reading is served with its real age and marked
stale rather than dropped, a newer recorded sample wins over an older cache,
a refusal is its own fact and outranks every window, and the scheduler's
`rate_limited` run becomes one refusal, not one per read.
"""

import json
import time
from datetime import datetime, timedelta, timezone

import pytest

from jstack_host import allowance


@pytest.fixture
def state(tmp_path, monkeypatch):
    monkeypatch.setattr(allowance, "STATE", tmp_path / "allowance.json")
    monkeypatch.setattr(allowance, "LOCK", tmp_path / ".lock")
    monkeypatch.setattr(allowance, "CLI_CONFIG", tmp_path / "claude.json")
    return tmp_path


def _cache(path, five=6, week=1, age_s=10, reset=None):
    fetched = int((time.time() - age_s) * 1000)
    reset = reset or (datetime.now(timezone.utc) + timedelta(hours=3)).isoformat()
    path.write_text(json.dumps({
        "hasVisitedExtraUsage": True,
        "cachedUsageUtilization": {
            "fetchedAtMs": fetched, "accountUuid": "x",
            "utilization": {
                "five_hour": {"utilization": five, "resets_at": reset},
                "seven_day": {"utilization": week, "resets_at": reset},
            },
        },
    }))


def test_nothing_sampled_is_null_never_zero(state):
    d = allowance.read()
    assert d["providers"] == {"claude": None, "codex": None}, d
    assert allowance.available() is False


def test_the_cli_cache_is_a_reading(state):
    _cache(state / "claude.json", five=8, week=56)
    assert allowance.available() is True
    p = allowance.read()["providers"]["claude"]
    assert p["source"] == "cli-cache" and p["label"] == "Claude"
    assert [(w["id"], w["label"], w["pct"], w["band"]) for w in p["windows"]] == [
        ("five_hour", "Session (5h)", 8.0, None),
        ("seven_day", "Week", 56.0, None)]
    assert p["stale"] is False and 0 <= p["age_seconds"] < 60
    assert p["refusal"] is None and p["worst_band"] is None


def test_an_old_reading_is_served_stale_not_dropped(state):
    _cache(state / "claude.json", five=61, age_s=6 * 3600)
    p = allowance.read()["providers"]["claude"]
    assert p["stale"] is True and p["age_seconds"] > 6 * 3600 - 5
    assert p["windows"][0]["pct"] == 61.0


def test_bands_follow_the_thresholds(state):
    _cache(state / "claude.json", five=97, week=100)
    p = allowance.read()["providers"]["claude"]
    assert [w["band"] for w in p["windows"]] == ["critical", "capped"]
    assert p["worst_band"] == "capped"
    _cache(state / "claude.json", five=80, week=10)
    assert allowance.read()["providers"]["claude"]["windows"][0]["band"] == "warn"


def test_the_newest_sample_wins(state):
    _cache(state / "claude.json", five=6, age_s=600)
    allowance.record("claude", [{"id": "five_hour", "label": "Session (5h)", "pct": 40}],
                     source="statusline", sampled_at=time.time() - 300)
    p = allowance.read()["providers"]["claude"]
    assert p["source"] == "statusline" and p["windows"][0]["pct"] == 40.0
    _cache(state / "claude.json", five=9, age_s=1)
    p = allowance.read()["providers"]["claude"]
    assert p["source"] == "cli-cache" and p["windows"][0]["pct"] == 9.0


def test_a_refusal_outranks_every_window_and_survives_a_fresh_cache(state):
    _cache(state / "claude.json", five=3)
    reset = (datetime.now(timezone.utc) + timedelta(minutes=30)).isoformat()
    allowance.note_refusal("claude", resets_at=reset, detail="hit your limit")
    p = allowance.read()["providers"]["claude"]
    assert p["refusal"]["active"] is True and p["worst_band"] == "refused"
    assert p["windows"][0]["pct"] == 3.0, "the cache reading still rides along"
    past = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
    allowance.note_refusal("claude", resets_at=past)
    assert allowance.read()["providers"]["claude"]["refusal"]["active"] is False


def test_the_scheduler_journal_is_a_headless_probe(state, tmp_path):
    st = tmp_path / "state.json"
    ran = int(time.time() * 1000)
    st.write_text(json.dumps({"job-a": {"last_status": "rate_limited",
                                        "last_run_at_ms": ran,
                                        "next_run_at_ms": ran + 3_600_000,
                                        "last_error": "rate_limited"},
                              "job-b": {"last_status": "ok", "last_run_at_ms": ran}}))
    assert allowance.sync_from_scheduler(st) is True
    assert allowance.sync_from_scheduler(st) is False, "the watermark holds"
    p = allowance.read()["providers"]["claude"]
    assert p["refusal"]["active"] is True and "rate-limited" in p["refusal"]["detail"]
    wrapped = tmp_path / "wrapped.json"
    wrapped.write_text(json.dumps({"jobs": {"job-c": {"last_status": "rate_limited",
                                                      "last_run_at_ms": ran + 5}}}))
    assert allowance.sync_from_scheduler(wrapped) is True, "either vintage of the file"


def test_an_unknown_source_is_refused(state):
    with pytest.raises(ValueError):
        allowance.record("claude", [], source="guess")


def test_a_clock_no_window_could_have_is_dropped_not_drawn(state, monkeypatch):
    """2026-09-09: both Claude bars read "resets in 1208d 21h".

    The reading itself was fine — the percentages were real. One far-future
    instant rode in on both windows and the app drew it verbatim, because
    neither end had an opinion about what a five-hour window's clock can say.
    The percentage must survive that; only the clock is refused.
    """
    monkeypatch.setattr(allowance, "_BAD_RESETS", set())
    monkeypatch.setattr(allowance.hostenv, "state_dir", lambda: state)
    far = datetime(2030, 1, 1, tzinfo=timezone.utc).isoformat()
    _cache(state / "claude.json", five=37, week=58, reset=far)
    p = allowance.read()["providers"]["claude"]
    assert [(w["pct"], w["resets_at"]) for w in p["windows"]] == [
        (37.0, None), (58.0, None)], "the reading stays, the clock goes"
    logged = [json.loads(ln) for ln in
              (state / "allowance_rejects.jsonl").read_text().splitlines()]
    assert [(e["source"], e["window"], e["resets_at"]) for e in logged] == [
        ("cli-cache", "five_hour", far), ("cli-cache", "seven_day", far)]
    allowance.read()
    assert len((state / "allowance_rejects.jsonl").read_text().splitlines()) == 2, \
        "a cache that stays wrong is reported once, not once per poll"


def test_an_epoch_clock_becomes_an_instant_the_app_can_read(state):
    """Claude Code hands the status line `resets_at` as epoch SECONDS, and a
    number is not a thing the app's `String?` can decode. Milliseconds too —
    whichever unit arrives, one ISO shape leaves."""
    secs = int(time.time()) + 3600
    allowance.record("claude", [
        {"id": "five_hour", "label": "Session (5h)", "pct": 8, "resets_at": secs},
        {"id": "seven_day", "label": "Week", "pct": 5, "resets_at": secs * 1000},
        {"id": "third", "label": "Third", "pct": 1, "resets_at": "not a clock"},
    ], source="statusline")
    want = datetime.fromtimestamp(secs, tz=timezone.utc).isoformat()
    assert [w["resets_at"] for w in
            allowance.read()["providers"]["claude"]["windows"]] == [want, want, None]


def test_a_cache_without_a_reading_is_absence(state):
    (state / "claude.json").write_text(json.dumps({"cachedUsageUtilization": {"utilization": {}}}))
    assert allowance.cli_cache_sample() is None
    (state / "claude.json").write_text("not json")
    assert allowance.cli_cache_sample() is None


def _codex_sample(path, at, pct=27, minutes=10080, limit_id='codex'):
    path.parent.mkdir(parents=True, exist_ok=True)
    row = {'type': 'event_msg', 'timestamp': datetime.fromtimestamp(at, timezone.utc).isoformat(),
           'payload': {'type': 'token_count', 'rate_limits': {
               'limit_id': limit_id, 'primary': {'used_percent': pct,
               'window_minutes': minutes, 'resets_at': at + 3600}, 'secondary': None}}}
    path.write_text(json.dumps(row) + '\n')


def test_codex_quota_appears_from_rollout_without_separate_connection(state, monkeypatch):
    monkeypatch.setattr(allowance, 'CODEX_SESSIONS', state / 'sessions')
    path = state / 'sessions' / 'rollout-new.jsonl'
    at = time.time() - 30
    _codex_sample(path, at, pct=0)
    p = allowance.read()['providers']['codex']
    assert p['source'] == 'rollout' and p['stale'] is False
    assert [(w['label'], w['pct']) for w in p['windows']] == [('Week', 0)]
    assert abs(p['sampled_at'] - at) < 0.00001
    assert allowance.available()


def test_codex_newest_sample_wins_and_uses_event_age(state, monkeypatch):
    monkeypatch.setattr(allowance, 'CODEX_SESSIONS', state / 'sessions')
    old = state / 'sessions' / 'rollout-old.jsonl'
    new = state / 'sessions' / 'rollout-new.jsonl'
    _codex_sample(old, time.time() - 7200, pct=90)
    _codex_sample(new, time.time() - 3600, pct=20, minutes=300)
    old.touch()  # file activity must not make an older sample look fresh
    p = allowance.read()['providers']['codex']
    assert p['stale'] and p['windows'][0]['pct'] == 20
    assert p['windows'][0]['label'] == 'Session (5h)'
    allowance.record('codex', [{'id': 'primary', 'label': 'Week', 'pct': 35}], source='poll')
    assert allowance.read()['providers']['codex']['windows'][0]['pct'] == 35


def test_codex_ignores_unrelated_quota_and_malformed_last_line(state, monkeypatch):
    monkeypatch.setattr(allowance, 'CODEX_SESSIONS', state / 'sessions')
    path = state / 'sessions' / 'rollout-new.jsonl'
    _codex_sample(path, time.time(), limit_id='other-product')
    assert allowance.read()['providers']['codex'] is None
    _codex_sample(path, time.time(), pct=15)
    with path.open('a') as fh:
        fh.write('{"partial":')
    assert allowance.read()['providers']['codex']['windows'][0]['pct'] == 15
