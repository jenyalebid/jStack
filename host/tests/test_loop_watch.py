"""The loop watch names what held the loop, captured while it was held."""

import asyncio
import json
import time

from jstack_host import loop_watch


def _blocks_the_loop():
    time.sleep(0.6)


def test_a_stall_is_logged_with_the_loop_threads_culprit(tmp_path, monkeypatch):
    log = tmp_path / "loop_stalls.jsonl"
    monkeypatch.setattr(loop_watch, "_log_path", lambda: log)
    monkeypatch.setattr(loop_watch, "_path", None)
    monkeypatch.setattr(loop_watch, "_started", False)
    loop_watch._recent.clear()

    async def run():
        loop_watch.start()
        await asyncio.sleep(0.3)
        _blocks_the_loop()
        await asyncio.sleep(0.5)

    asyncio.run(run())
    rec = json.loads(log.read_text().splitlines()[0])
    assert 500 <= rec["stall_ms"] <= 1500
    loop_stack = next(v for k, v in rec["samples"][0]["stacks"].items() if k.startswith("LOOP"))
    assert loop_stack[-1].endswith("_blocks_the_loop")
    s = loop_watch.stats()
    assert s["stalls"] == 1 and s["worst_ms"] >= 500


def test_an_unblocked_loop_logs_nothing(tmp_path, monkeypatch):
    log = tmp_path / "loop_stalls.jsonl"
    monkeypatch.setattr(loop_watch, "_log_path", lambda: log)
    monkeypatch.setattr(loop_watch, "_path", None)
    monkeypatch.setattr(loop_watch, "_started", False)
    loop_watch._recent.clear()

    async def run():
        loop_watch.start()
        await asyncio.sleep(0.6)

    asyncio.run(run())
    assert not log.exists()
    assert loop_watch.stats()["stalls"] == 0
