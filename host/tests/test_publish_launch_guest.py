"""The client build brings its own launch-check guest, and never spends a build
number on a guest that is not there."""
import json
import stat
from pathlib import Path

import pytest

from jstack_host import publish_release, release_manifest as releases


def _script(path: Path, body: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body)
    path.chmod(path.stat().st_mode | stat.S_IEXEC)
    return path


def _work(tmp_path: Path, *, preflight_ok: bool = True, build_ok: bool = True) -> Path:
    """A build work dir whose client carries stand-in release scripts."""
    work = tmp_path / "build-x"
    app = work / "Projects/client/jRemote-Code/jRemote"
    (app / "jRemote.xcodeproj").mkdir(parents=True)
    (app / "jRemote.xcodeproj/project.pbxproj").write_text("CURRENT_PROJECT_VERSION = 119;\n")
    events = tmp_path / "events"
    _script(app / "Scripts/check-launch.py",
            "import os, sys\n"
            f"open({str(events)!r}, 'a').write('preflight ' + os.environ.get('JREMOTE_RELEASE_TEST_HOST', '') + '\\n')\n"
            f"sys.exit({0 if preflight_ok else 1})\n")
    _script(app / "release-mac.sh",
            f"echo \"build $JREMOTE_RELEASE_TEST_HOST $2\" >> {events}\n"
            f"exit {0 if build_ok else 1}\n")
    return work


def _config(tmp_path: Path, *, up_ok: bool = True) -> dict:
    events = tmp_path / "events"
    up = _script(tmp_path / "guest-up",
                 f"#!/bin/bash\necho up >> {events}\necho 'booting…'\n"
                 + ("echo admin@192.168.64.9\n" if up_ok else "exit 1\n"))
    down = _script(tmp_path / "guest-down", f"#!/bin/bash\necho down >> {events}\n")
    return {"candidates_dir": str(tmp_path / "candidates"),
            "client_launch_guest": {"up": [str(up)], "down": [str(down)]}}


def _events(tmp_path: Path) -> list[str]:
    return (tmp_path / "events").read_text().splitlines()


def _reserved(tmp_path: Path) -> int | None:
    state = tmp_path / "candidates/build-number.json"
    return json.loads(state.read_text())["build"] if state.exists() else None


@pytest.fixture(autouse=True)
def _no_operator_guest(monkeypatch):
    monkeypatch.delenv("JREMOTE_RELEASE_TEST_HOST", raising=False)


def test_one_command_boots_the_guest_builds_against_it_and_tears_it_down(tmp_path):
    publish_release.build_candidate_client(_config(tmp_path), _work(tmp_path), "notes")
    assert _events(tmp_path) == ["up", "preflight admin@192.168.64.9",
                                 "build admin@192.168.64.9 120", "down"]


def test_a_guest_that_does_not_boot_costs_no_build_number(tmp_path):
    with pytest.raises(releases.ReleaseError, match="did not come up"):
        publish_release.build_candidate_client(_config(tmp_path, up_ok=False), _work(tmp_path), "notes")
    assert _reserved(tmp_path) is None
    assert _events(tmp_path) == ["up", "down"]


def test_a_guest_that_fails_preflight_costs_no_build_number(tmp_path):
    with pytest.raises(releases.ReleaseError, match="preflight"):
        publish_release.build_candidate_client(_config(tmp_path), _work(tmp_path, preflight_ok=False), "notes")
    assert _reserved(tmp_path) is None
    assert _events(tmp_path) == ["up", "preflight admin@192.168.64.9", "down"]


def test_a_failed_build_still_tears_the_guest_down(tmp_path):
    with pytest.raises(releases.ReleaseError, match="client build failed"):
        publish_release.build_candidate_client(_config(tmp_path), _work(tmp_path, build_ok=False), "notes")
    assert _events(tmp_path)[-1] == "down"


def test_no_guest_at_all_is_refused_before_a_number_is_reserved(tmp_path):
    config = {"candidates_dir": str(tmp_path / "candidates")}
    with pytest.raises(releases.ReleaseError, match="preflight"):
        publish_release.build_candidate_client(config, _work(tmp_path, preflight_ok=False), "notes")
    assert _reserved(tmp_path) is None


def test_an_operator_named_guest_is_used_and_left_alone(tmp_path, monkeypatch):
    monkeypatch.setenv("JREMOTE_RELEASE_TEST_HOST", "admin@10.0.0.5")
    publish_release.build_candidate_client(_config(tmp_path), _work(tmp_path), "notes")
    assert _events(tmp_path) == ["preflight admin@10.0.0.5", "build admin@10.0.0.5 120"]
