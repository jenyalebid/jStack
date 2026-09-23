"""Fault injection must target the service owner, including a sealed runtime."""
import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest


@pytest.fixture
def fault():
    path = Path(__file__).resolve().parents[1] / "tools/managed_update_fault.py"
    spec = importlib.util.spec_from_file_location("update_fault_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_native_updater_is_resolved_from_its_launchd_service(fault, monkeypatch, tmp_path):
    calls = []
    def launch(argv, **kwargs):
        calls.append(argv)
        return SimpleNamespace(stdout="service = {\n\tpid = 4123\n}\n")
    process = SimpleNamespace(uids=lambda: SimpleNamespace(real=501),
                              cmdline=lambda: ["JStackRuntime", "updater"])
    monkeypatch.setattr(fault.os, "getuid", lambda: 501)
    monkeypatch.setattr(fault.subprocess, "run", launch)
    def lookup(pid):
        assert pid == 4123
        return process
    monkeypatch.setattr(fault.psutil, "Process", lookup)
    assert fault.updater_process({"service_model": "app"}, tmp_path) is process
    assert calls == [["/bin/launchctl", "print", "gui/501/live.jstack.hub.updater"]]


@pytest.mark.parametrize("owner,command", [(0, ["JStackRuntime", "updater"]),
                                           (501, ["JStackRuntime", "host"])])
def test_native_updater_refuses_a_different_owner_or_role(
        fault, monkeypatch, tmp_path, owner, command):
    monkeypatch.setattr(fault.os, "getuid", lambda: 501)
    monkeypatch.setattr(fault.subprocess, "run",
                        lambda *a, **k: SimpleNamespace(stdout="pid = 4123\n"))
    monkeypatch.setattr(fault.psutil, "Process", lambda pid: SimpleNamespace(
        uids=lambda: SimpleNamespace(real=owner), cmdline=lambda: command))
    with pytest.raises(RuntimeError, match="unexpected process"):
        fault.updater_process({"service_model": "app"}, tmp_path)
