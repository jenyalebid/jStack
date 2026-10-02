"""parse_session parses a transcript once per version of the file."""

import json
import os

from jstack_host import messages

SID = "11111111-2222-3333-4444-555555555555"


def _line(text):
    return json.dumps({"type": "user", "message": {"role": "user", "content": text},
                       "timestamp": "2026-10-02T10:00:00Z"}) + "\n"


def test_an_unchanged_file_is_not_parsed_again_and_a_changed_one_is(tmp_path, monkeypatch):
    f = tmp_path / f"{SID}.jsonl"
    f.write_text(_line("first"))
    monkeypatch.setattr(messages, "_find_session_file", lambda sid: f)
    monkeypatch.setattr(messages, "_parsed", {})
    calls = []
    real = messages._parse_file
    monkeypatch.setattr(messages, "_parse_file", lambda p: calls.append(p) or real(p))

    a = messages.parse_session(SID)
    b = messages.parse_session(SID)
    assert [m["text"] for m in a["messages"]] == ["first"]
    assert b is a and len(calls) == 1

    with open(f, "a") as fh:
        fh.write(_line("second"))
    st = f.stat()
    os.utime(f, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000))
    c = messages.parse_session(SID)
    assert [m["text"] for m in c["messages"]] == ["first", "second"]
    assert len(calls) == 2
