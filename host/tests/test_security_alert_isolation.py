"""The suite must never deliver synthetic security events to a real user.

Two layers, and the order matters. The conftest sink is the convenience — it
keeps alert payloads inspectable and covers this checkout. `in_test_process()`
inside the shipped function is the *guarantee*: a checkout whose conftest
predates the sink, a release staged under another tree, a suite run from a
worktree nobody has rebased in three weeks — none of them can opt out of it.
The second layer's absence is what an embedding host paid for in 2026-09:
100 lockout alerts from fixture addresses over twelve days, every one of them
paging a person, and the last pair read as an intrusion.
"""
from threading import Thread
from types import SimpleNamespace

from jstack_host import hostenv


def _forbidden(_body):
    raise AssertionError("a test process reached the live notification boundary")


def test_security_alerts_cannot_reach_the_machine_notifier(monkeypatch, _isolated_security_alerts):
    monkeypatch.setattr(hostenv, "profile", lambda: SimpleNamespace(security_alert=_forbidden))
    body = "synthetic isolation probe"
    thread = Thread(target=hostenv.security_alert, args=(body,))
    thread.start()
    thread.join(timeout=2)
    assert not thread.is_alive()
    assert body in _isolated_security_alerts


def test_the_shipped_alert_path_delivers_nothing_from_a_test_process(
        shipped_security_alert, monkeypatch, capsys):
    """The function as it ships, with no sink patched over it, reaching for a
    profile that raises if it is ever asked to send. Silence here is the whole
    fix: it is what makes a stale conftest harmless."""
    monkeypatch.setattr(hostenv, "profile", lambda: SimpleNamespace(security_alert=_forbidden))

    shipped_security_alert("synthetic lockout probe")

    assert "not delivered" in capsys.readouterr().out


def test_the_refusal_outlives_the_test_that_tripped_it(
        shipped_security_alert, monkeypatch):
    """`PYTEST_CURRENT_TEST` is gone the instant a test ends, and the alarm is
    raised on a daemon thread — so a lockout tripped at the end of a test can
    reach the send with the variable already unset. The refusal has to hold on
    the process, not on the test.

    Recorded rather than raised: an AssertionError inside a thread dies with
    the thread, so a test that only joins would pass through the delivery it
    exists to forbid."""
    delivered = []
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    monkeypatch.setattr(hostenv, "profile",
                        lambda: SimpleNamespace(security_alert=delivered.append))

    assert hostenv.in_test_process()
    thread = Thread(target=shipped_security_alert, args=("probe after the test",))
    thread.start()
    thread.join(timeout=2)
    assert not thread.is_alive()
    assert delivered == []
