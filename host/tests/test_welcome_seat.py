"""`welcome` opens the seat, not the workspace above it.

The install's first session is the one nobody chooses a directory for, so
wherever it lands is where that Mac's owner learns their agent works. Landing
it at the agent root put it outside the seat every later session opens — a
different pad, a different project dir, and a chat count (which counts `chat/`
sessions only) that reads 0 with the session sitting right there.
"""
import pytest

from jstack_host import cli, hostenv


@pytest.fixture
def tree(tmp_path, monkeypatch):
    """An agents root with one seated agent and one bare one."""
    agents = tmp_path / "Agents"
    (agents / "Kate" / "chat").mkdir(parents=True)
    (agents / "Kate" / "CLAUDE.md").write_text("# Kate\n")
    (agents / "Kate" / "chat" / "CLAUDE.md").write_text("# Kate · chat\n")
    (agents / "Jerry").mkdir(parents=True)
    (agents / "Jerry" / "CLAUDE.md").write_text("# Jerry\n")
    monkeypatch.setenv("JREMOTE_INSTANCE_ROOT", str(agents))
    hostenv.reset_profile()
    yield agents
    hostenv.reset_profile()


def _welcome(monkeypatch, agent):
    """Run the verb with its two side effects captured: the cwd it spawns in."""
    spawned = {}
    monkeypatch.setattr(cli, "_adopt", lambda args: None)
    monkeypatch.setattr("jstack_host.desk.create",
                        lambda cwd, **kw: spawned.setdefault("cwd", cwd) or "sid-1")
    monkeypatch.setattr("jstack_host.desk.open_thread", lambda sid, cwd="": True)
    assert cli.main(["welcome", "--agent", agent]) == 0
    return spawned["cwd"]


def test_welcome_lands_in_the_chat_seat(tree, monkeypatch):
    assert _welcome(monkeypatch, "kate") == str(tree / "Kate" / "chat")


def test_welcome_falls_back_to_the_root_of_a_seatless_agent(tree, monkeypatch):
    # The shape older installs left behind. It still has to open something,
    # and the agent root is the only directory that exists.
    assert _welcome(monkeypatch, "jerry") == str(tree / "Jerry")
