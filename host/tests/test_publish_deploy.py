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
