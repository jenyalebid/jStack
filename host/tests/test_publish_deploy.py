"""The deployer talks to the hub with the hub's own credential."""
import json
from pathlib import Path

import pytest

from jstack_host import hostenv, publish_release


class _Answer:
    def __init__(self, body):
        self.body = body

    def raise_for_status(self):
        pass

    def json(self):
        return self.body


class _Client:
    def __init__(self, calls):
        self.calls = calls

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def get(self, url, headers=None):
        self.calls.append(("GET", url, headers["Authorization"]))
        return _Answer({"release": "r1", "machines": [
            {"machine": "m1", "supervisor": True, "state": "current"}]})

    def post(self, url, headers=None, json=None):
        self.calls.append(("POST", url, headers["Authorization"]))
        self.requests = getattr(self, "requests", []) + [json["request_id"]]
        return _Answer({"jobs": [{"id": "j1"}]})


def test_deploy_uses_the_embedded_hubs_state_dir(tmp_path, monkeypatch):
    hub_state = tmp_path / "hub-state"
    hub_state.mkdir()
    marker = tmp_path / "embedded.json"
    marker.write_text(json.dumps({"server": "the dashboard", "port": 9090,
                                  "state_dir": str(hub_state), "root": str(tmp_path / "nowhere")}))
    monkeypatch.setenv("JREMOTE_EMBED_MARKER", str(marker))
    monkeypatch.delenv("JREMOTE_STATE_DIR", raising=False)
    monkeypatch.delenv("JREMOTE_PROFILE_MODULE", raising=False)
    monkeypatch.setattr(publish_release, "sys", type("S", (), {"modules": {}}))
    from jstack_host import install_host
    monkeypatch.setattr(install_host, "installed_environment", lambda path=None: {})
    hostenv.reset_profile()
    resolved = []

    def internal_token():
        resolved.append(hostenv.state_dir())
        return "hub-secret"

    from jstack_host import devices
    monkeypatch.setattr(devices, "internal_token", internal_token)
    import httpx
    calls = []
    monkeypatch.setattr(httpx, "Client", lambda **kw: _Client(calls))
    try:
        result = publish_release.deploy("r1", port=9090, timeout=1, poll=0)
    finally:
        hostenv.reset_profile()
    assert resolved == [hub_state]
    assert {auth for _, _, auth in calls} == {"Bearer hub-secret"}
    assert result["states"] == {"m1": "current"} and not result["unreached"]


def test_deploy_refuses_a_hub_that_revoked_its_own_credential(monkeypatch):
    from jstack_host import devices, install_host
    monkeypatch.setattr(install_host, "adopt_installed_environment", lambda path=None: None)
    monkeypatch.setattr(publish_release, "sys", type("S", (), {"modules": {}}))
    monkeypatch.setattr(devices, "internal_token", lambda: "")
    with pytest.raises(ValueError, match="revoked its own internal credential"):
        publish_release.hub_token()


class _RestartingClient(_Client):
    """A hub that is itself in the fleet: its own apply restarts the server
    under the poller, so the inventory that follows the queue is refused."""

    def __init__(self, calls, refusals):
        super().__init__(calls)
        self.refusals = refusals

    def get(self, url, headers=None):
        if self.calls and self.refusals:
            self.refusals -= 1
            import httpx
            raise httpx.ConnectError("[Errno 61] Connection refused")
        return super().get(url, headers=headers)


def _hub_credential(monkeypatch):
    from jstack_host import devices, install_host
    monkeypatch.setattr(install_host, "adopt_installed_environment", lambda path=None: None)
    monkeypatch.setattr(publish_release, "sys", type("S", (), {"modules": {}}))
    monkeypatch.setattr(devices, "internal_token", lambda: "hub-secret")


def test_deploy_outlasts_the_hubs_own_restart(monkeypatch):
    _hub_credential(monkeypatch)
    import httpx
    calls = []
    monkeypatch.setattr(httpx, "Client", lambda **kw: _RestartingClient(calls, refusals=2))
    clients = []
    monkeypatch.setattr(httpx, "Client", lambda **kw: clients.append(_RestartingClient(calls, refusals=2)) or clients[-1])
    result = publish_release.deploy("r1", port=9090, timeout=5, poll=0)
    assert result["states"] == {"m1": "current"}
    assert clients[-1].refusals == 0, "both refused polls were retried"
    assert [m for m, _, _ in calls] == ["GET", "POST", "GET"]


def test_deploy_gives_up_on_a_hub_that_stays_down(monkeypatch):
    _hub_credential(monkeypatch)
    import httpx
    calls = []
    monkeypatch.setattr(httpx, "Client", lambda **kw: _RestartingClient(calls, refusals=10 ** 6))
    with pytest.raises(httpx.ConnectError):
        publish_release.deploy("r1", port=9090, timeout=0, poll=0)


def test_each_deploy_run_is_its_own_request(monkeypatch):
    """The hub answers a repeated request id with the job it already named,
    so a deploy re-run after a failed machine was repaired must not reuse one."""
    _hub_credential(monkeypatch)
    import httpx
    clients = []
    monkeypatch.setattr(httpx, "Client", lambda **kw: clients.append(_Client([])) or clients[-1])
    publish_release.deploy("r1", port=9090, timeout=1, poll=0)
    monkeypatch.setattr(publish_release.time, "strftime", lambda fmt: "later")
    publish_release.deploy("r1", port=9090, timeout=1, poll=0)
    first, second = clients[0].requests[0], clients[1].requests[0]
    assert first.startswith("release-r1-") and second == "release-r1-later" and first != second


def _client_repo(tmp_path: Path, build: int = 106) -> Path:
    """A checkout shaped like jRemote's, with an origin to push to."""
    import subprocess
    origin, repo = tmp_path / "origin.git", tmp_path / "client"
    subprocess.run(["git", "init", "--bare", "-q", str(origin)], check=True)
    subprocess.run(["git", "clone", "-q", str(origin), str(repo)], check=True)
    project = repo / "jRemote-Code/jRemote/jRemote.xcodeproj/project.pbxproj"
    project.parent.mkdir(parents=True)
    project.write_text(f"CURRENT_PROJECT_VERSION = {build};\n"
                       "MARKETING_VERSION = 1.0;\n"
                       f"CURRENT_PROJECT_VERSION = {build};\n")
    (repo / "neighbour.txt").write_text("someone else's work\n")
    git = ["git", "-C", str(repo), "-c", "user.email=t@t", "-c", "user.name=t"]
    subprocess.run([*git, "add", "-A"], check=True)
    subprocess.run([*git, "commit", "-qm", "init"], check=True)
    subprocess.run([*git, "push", "-q", "-u", "origin", "HEAD:refs/heads/main"], check=True)
    subprocess.run(["git", "-C", str(repo), "branch", "-q",
                    "--set-upstream-to=origin/main"], check=True)
    return repo


def _version(repo: Path, ref: str = "HEAD") -> str:
    from jstack_host.update_macos import command
    return command(["git", "-C", str(repo), "show",
                    f"{ref}:jRemote-Code/jRemote/jRemote.xcodeproj/project.pbxproj"])


def test_a_shipped_build_number_lands_in_the_source_it_was_cut_from(tmp_path):
    """Without this the project keeps a number below what is on people's Macs,
    and every Xcode build after a release stamps a downgrade."""
    repo = _client_repo(tmp_path)
    result = publish_release.stamp_client_build(repo, 109, "Shipped in r1.")
    assert "109" in result and "pushed" in result
    assert _version(repo).count("CURRENT_PROJECT_VERSION = 109;") == 2
    assert _version(repo, "origin/main").count("CURRENT_PROJECT_VERSION = 109;") == 2


def test_the_stamp_commits_its_own_file_and_nobody_elses(tmp_path):
    """The checkout is shared and every session holds its own index."""
    import subprocess
    repo = _client_repo(tmp_path)
    (repo / "neighbour.txt").write_text("edited by another session\n")
    publish_release.stamp_client_build(repo, 109, "Shipped in r1.")
    changed = subprocess.run(["git", "-C", str(repo), "show", "--name-only",
                              "--format=", "HEAD"], capture_output=True, text=True).stdout
    assert changed.split() == ["jRemote-Code/jRemote/jRemote.xcodeproj/project.pbxproj"]
    assert (repo / "neighbour.txt").read_text() == "edited by another session\n"


def test_a_number_already_reached_is_not_stamped_again(tmp_path):
    repo = _client_repo(tmp_path, build=111)
    assert "already carries build 111" in publish_release.stamp_client_build(repo, 109, "")


def test_an_unstampable_project_says_so_rather_than_passing_quietly(tmp_path):
    """Silence here is the whole defect: five builds shipped without anyone
    seeing that the source never caught up."""
    repo = _client_repo(tmp_path)
    project = repo / "jRemote-Code/jRemote/jRemote.xcodeproj/project.pbxproj"
    project.write_text(project.read_text().replace("= 106;\nMARKETING", "= 107;\nMARKETING"))
    assert "WARNING" in publish_release.stamp_client_build(repo, 112, "")
    assert _version(repo).count("CURRENT_PROJECT_VERSION = 106;") == 2
    assert "WARNING" in publish_release.stamp_client_build(tmp_path / "nothing", 112, "")
    import subprocess
    subprocess.run(["git", "-C", str(repo), "-c", "user.email=t@t", "-c", "user.name=t",
                    "commit", "-qam", "targets drift"], check=True)
    assert "disagree" in publish_release.stamp_client_build(repo, 112, "")
    assert "CURRENT_PROJECT_VERSION = 112;" not in _version(repo)
