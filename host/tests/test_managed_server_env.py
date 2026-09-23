"""The tmux server a managed session spawns never carries the embedding
Hub interpreter's private variables — every agent shell inherits that
server's environment, and a leaked PYTHONPATH makes any python those shells
run (session hooks first) import the app's compiled extensions ahead of its
own."""
from jstack_host import managed


def test_server_env_drops_interpreter_private_vars(monkeypatch):
    monkeypatch.setenv("PYTHONPATH", "/Applications/jStack Hub.app/Contents/Resources/packages")
    monkeypatch.setenv("PYTHONDONTWRITEBYTECODE", "1")
    monkeypatch.setenv("PYTHONHOME", "/nowhere")
    monkeypatch.setenv("HOME", "/Users/someone")
    env = managed._server_env()
    assert "PYTHONPATH" not in env
    assert "PYTHONDONTWRITEBYTECODE" not in env
    assert "PYTHONHOME" not in env
    assert env["HOME"] == "/Users/someone"
    assert env["PATH"] == managed._PATH


def test_server_env_is_a_copy(monkeypatch):
    monkeypatch.setenv("JREMOTE_PROBE", "x")
    env = managed._server_env()
    env["JREMOTE_PROBE"] = "changed"
    assert managed._server_env()["JREMOTE_PROBE"] == "x"
