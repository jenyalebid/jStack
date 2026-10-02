"""A seat is a directory holding AGENTS.md — or CLAUDE.md, until it migrates (#353)."""
import sys
from pathlib import Path

from jstack_host.seats import is_seat

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "plugins/jstack"))
import root  # noqa: E402


def test_agents_md_marks_a_seat(tmp_path):
    (tmp_path / "AGENTS.md").write_text("# seat\n")
    assert is_seat(tmp_path)
    assert root.is_agent(tmp_path)


def test_a_seat_not_yet_migrated_still_counts(tmp_path):
    (tmp_path / "CLAUDE.md").write_text("# seat\n")
    assert is_seat(tmp_path)
    assert root.is_agent(tmp_path)


def test_neither_file_is_no_seat(tmp_path):
    (tmp_path / "README.md").write_text("# not a seat\n")
    assert not is_seat(tmp_path)
    assert not root.is_agent(tmp_path)
