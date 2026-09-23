"""Execute startup helpers with the sealed app's space-bearing executable path."""
import json
import subprocess
import sys

import pytest

from jstack_host import managed


@pytest.mark.parametrize("helper,screen", [
    ("_auto_accept_bypass", "Yes, I accept"),
    ("_auto_skip_codex_update", "Skip until next version"),
    ("_nudge_when_ready", "bypass permissions on"),
    ("attach_command", ""),
])
def test_shell_helpers_execute_bundled_tmux(tmp_path, monkeypatch, helper, screen):
    binary = tmp_path / "jStack Hub.app" / "tmux"
    binary.parent.mkdir()
    log = tmp_path / "calls.jsonl"
    binary.write_text(f"#!{sys.executable}\nimport json,sys\n"
                      f"with open({str(log)!r}, 'a') as f: f.write(json.dumps(sys.argv[1:])+'\\n')\n"
                      f"if 'capture-pane' in sys.argv: print({screen!r})\n")
    binary.chmod(0o755)
    monkeypatch.setattr(managed, "_TMUX", str(binary))
    monkeypatch.setattr(managed, "_SOCK", "socket with spaces")
    processes = []
    real_popen = subprocess.Popen
    def spawn(*args, **kwargs):
        process = real_popen(*args, **kwargs)
        processes.append(process)
        return process
    monkeypatch.setattr(managed.subprocess, "Popen", spawn)
    try:
        if helper == "attach_command":
            subprocess.run(["bash", "-c", managed.attach_command("12345678-abcd")], check=True)
        elif helper == "_nudge_when_ready":
            managed._nudge_when_ready("pane with spaces", "hello")
        else:
            getattr(managed, helper)("pane with spaces")
        for process in processes:
            assert process.wait(timeout=3) == 0
        calls = [json.loads(line) for line in log.read_text().splitlines()]
        assert all(call[:2] == ["-L", "socket with spaces"] for call in calls)
        if helper != "attach_command":
            assert any("send-keys" in call and "pane with spaces" in call for call in calls)
    finally:
        for process in processes:
            if process.poll() is None:
                process.terminate()
                process.wait(timeout=3)
