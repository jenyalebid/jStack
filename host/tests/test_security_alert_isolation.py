"""The suite must never deliver synthetic security events to a real user."""
from threading import Thread
from types import SimpleNamespace

from jstack_host import hostenv


def test_security_alerts_cannot_reach_the_machine_notifier(monkeypatch, _isolated_security_alerts):
    def forbidden(_body):
        raise AssertionError("test reached the live notification boundary")

    monkeypatch.setattr(hostenv, "profile", lambda: SimpleNamespace(security_alert=forbidden))
    body = "synthetic isolation probe"
    thread = Thread(target=hostenv.security_alert, args=(body,))
    thread.start()
    thread.join(timeout=2)
    assert not thread.is_alive()
    assert body in _isolated_security_alerts
