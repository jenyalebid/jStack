import json
from pathlib import Path

import pytest

from jstack_host import local_service, service_catalog


def job(**changes):
    return {"Label": "old.example.health", "ProgramArguments": ["/bin/echo", "done"],
            "StartInterval": 300, "RunAtLoad": True, **changes}


def test_catalog_keeps_launchd_triggers_without_exposing_arguments():
    definition = job(EnvironmentVariables={"EXAMPLE_SECRET": "must-not-be-published"})
    plists, manifest = service_catalog.definitions({"health": definition})
    record = plists["live.jstack.automation.health.plist"]
    assert record["StartInterval"] == 300
    assert record["RunAtLoad"] is True
    assert record["ProgramArguments"] == ["JStackRuntime", "local", "health"]
    assert "must-not-be-published" not in json.dumps([plists, manifest])
    assert "old.example.health" not in json.dumps([plists, manifest])
    assert manifest["health"]["job_sha256"] == service_catalog.digest(definition)


@pytest.mark.parametrize("changes", [{"UserName": "root"}, {"ProgramArguments": ["relative"]},
                                     {"WorkingDirectory": "relative"}, {"Sockets": {}},
                                     {"Label": None}, {"EnvironmentVariables": {"KEY": 3}},
                                     {"StandardOutPath": "relative"}, {"WatchPaths": ["/private/path"]},
                                     {"KeepAlive": {"PathState": {"/private/path": True}}},
                                     {"ProcessType": "private data"},
                                     {"StartCalendarInterval": {"Hour": "private data"}}])
def test_unknown_or_privileged_definitions_are_rejected(changes):
    with pytest.raises(ValueError):
        service_catalog.definitions({"health": job(**changes)})


def test_duplicate_service_cannot_be_adopted_twice():
    with pytest.raises(ValueError, match="duplicate legacy"):
        service_catalog.definitions({"first": job(), "second": job()})


def test_real_capability_runs_under_exact_approved_definition(monkeypatch, tmp_path):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "must-not-leak")
    definition = job(ProgramArguments=["/bin/sh", "-c", 'test -z "$CLAUDE_CODE_SESSION_ID" && printf capability-output'])
    settings = tmp_path / ".local/state/jremote/automation-settings.json"
    settings.parent.mkdir(parents=True)
    settings.write_text(json.dumps({"health": definition}))
    (tmp_path / "automation-catalog.json").write_text(json.dumps(service_catalog.definitions({"health": definition})[1]))
    assert local_service.run(tmp_path, "health") == 0
    assert (settings.parent / "logs/health.log").read_text() == "capability-output"
    assert (settings.parent / "logs/health.log").stat().st_mode & 0o777 == 0o600
    definition["ProgramArguments"] = ["/bin/echo", "tampered"]
    settings.write_text(json.dumps({"health": definition}))
    with pytest.raises(ValueError, match="differs from the signed catalog"):
        local_service.run(tmp_path, "health")


def test_local_capabilities_refuse_root(monkeypatch, tmp_path):
    monkeypatch.setattr(local_service.os, "geteuid", lambda: 0)
    with pytest.raises(PermissionError):
        local_service.run(tmp_path, "health")


def test_local_capability_reads_selected_private_configuration(monkeypatch, tmp_path):
    from jstack_host import service_settings
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    private = tmp_path / "private-data/automation.json"
    private.parent.mkdir()
    definition = job()
    private.write_text(json.dumps({"health": definition}))
    monkeypatch.setattr(service_settings, "read", lambda: {"automation_settings": str(private)})
    (tmp_path / "automation-catalog.json").write_text(json.dumps(service_catalog.definitions({"health": definition})[1]))
    assert local_service.run(tmp_path, "health") == 0
    assert not (tmp_path / ".local/state/jremote/automation-settings.json").exists()


def test_embedding_overlay_checks_hub_and_does_not_write_bytecode(monkeypatch, tmp_path):
    from jstack_host import app_services, service_settings
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    app = tmp_path / "Hub.app"
    (app / "Contents/Resources/packages").mkdir(parents=True)
    definition = job(ProgramArguments=["/bin/sh", "-c", 'printf "%s\\n%s" "$PYTHONPATH" "$PYTHONDONTWRITEBYTECODE"'],
                     EnvironmentVariables={"PYTHONPATH": "/approved/embedding"})
    settings = tmp_path / ".local/state/jremote/automation-settings.json"
    settings.parent.mkdir(parents=True)
    settings.write_text(json.dumps({"health": definition}))
    (tmp_path / "automation-catalog.json").write_text(json.dumps(service_catalog.definitions({"health": definition})[1]))
    checked = []
    monkeypatch.setattr(app_services, "verify", lambda path: checked.append(path))
    monkeypatch.setattr(service_settings, "read", lambda: {"host_capability": "health", "app": str(app)})
    assert local_service.run(tmp_path, "health") == 0
    assert checked == [app]
    output = (settings.parent / "logs/health.log").read_text()
    assert output == str(app / "Contents/Resources/packages") + ":/approved/embedding\n1"
