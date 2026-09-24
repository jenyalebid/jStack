"""Leaving a parent hub — every step, including the ones on the other machine.

Run entirely against a fake root (`root=tmp_path`) and a recording poster, so
the real `/Library/LaunchDaemons` is never touched and no network call is made.
The thing being pinned is that detaching is *both ends*: the step list is the
contract, and a step that could not run has to be visible as itself rather than
folded into a pass or a fail.
"""

import json

import pytest

from jstack_host import detach_parent, grants
from jstack_host.attach_parent import PARENT_RECORD


def _leaf_installed(root):
    """The files `install_leaf.sh` leaves behind, under a fake root."""
    for raw in detach_parent.LEAF_PATHS:
        target = root / raw.lstrip("/")
        if raw.endswith((".plist", ".conf")):
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("x")
        else:
            target.mkdir(parents=True, exist_ok=True)
            (target / "keep").write_text("x")


def _record(state, url="http://studio.local:9090", device_id="dev1",
            token="jr1.dev1.secret"):
    state.mkdir(parents=True, exist_ok=True)
    (state / PARENT_RECORD).write_text(json.dumps(
        {"parent_url": url, "device_id": device_id, "token": token}))


class _Runner:
    """Stands in for subprocess.run — records argv, always succeeds."""

    def __init__(self):
        self.calls = []

    def __call__(self, argv, **kw):
        self.calls.append(argv)
        return type("P", (), {"returncode": 0, "stdout": "", "stderr": ""})()


def _poster(status=200, calls=None):
    def post(url, token):
        if calls is not None:
            calls.append((url, token))
        return status, {}
    return post


def _step(result, name):
    return next((s for s in result["steps"] if s["step"] == name), None)


def test_detach_revokes_the_grants_first_and_says_how_many(tmp_path):
    grants.issue("http://studio.local:9090")
    grants.issue("http://studio.local:9090")
    result = detach_parent.detach(root=tmp_path, state=tmp_path / "state",
                                  runner=_Runner(), poster=_poster(), sudo=False)
    assert result["steps"][0]["step"] == "grants"
    assert "revoked 2 grants" in result["steps"][0]["note"]
    assert grants.issued() and all(r["revoked_at"] for r in grants.issued())


def test_detach_never_revives_legacy_credentials(tmp_path, monkeypatch):
    from jstack_host import devices, managed_access, auth
    from jstack_host.store import SessionStore
    database = tmp_path / "authority.sqlite"
    store = SessionStore(db_path=database)
    monkeypatch.setattr(grants, "_store", lambda: store)
    monkeypatch.setattr(devices, "_store", lambda: store)
    state = tmp_path / "state"
    _record(state)
    monkeypatch.setattr(managed_access, "is_leaf", lambda: (state / PARENT_RECORD).exists())
    store.add_device("old-delegated", "old", devices._hash("old-secret"))
    grant = grants.issue("parent")
    _, secret = grants.parse(grant)
    store.add_device("projected", "projected", devices._hash("projected-secret"))
    store.bind_device_authority("projected", grants._hash(secret), "owner")
    store.add_device(devices.INTERNAL_ID, "internal", devices._hash("internal-secret"))
    monkeypatch.setattr(auth, "_expected_token", lambda: "old-file-token")
    assert managed_access.device_allowed("old-delegated") is False
    detach_parent.detach(root=tmp_path, state=state, runner=_Runner(), poster=_poster(), sudo=False)
    assert not (state / PARENT_RECORD).exists()
    store = SessionStore(db_path=database)
    assert managed_access.device_allowed("old-delegated") is False
    assert devices.authenticate("jr1.old-delegated.old-secret") is None
    assert devices.authenticate("jr1.projected.projected-secret") is None
    assert grants.authenticate(grant) is None
    assert devices.authenticate("old-file-token") is None
    assert store.device(devices.LEGACY_ID)["revoked_at"] is not None
    assert store.device(devices.INTERNAL_ID)["revoked_at"] is None
    store.add_device("new-independent", "new", devices._hash("new-secret"))
    detach_parent.detach(root=tmp_path, state=state, runner=_Runner(), poster=_poster(), sudo=False)
    assert store.device("new-independent")["revoked_at"] is None


def test_revocation_failure_preserves_attachment(tmp_path, monkeypatch):
    store = grants._store()
    def fail():
        raise OSError("database unavailable")
    monkeypatch.setattr(store, "revoke_parent_authority", fail)
    state = tmp_path / "state"
    _record(state)
    calls, runner = [], _Runner()
    with pytest.raises(detach_parent.DetachError, match="attachment preserved"):
        detach_parent.detach(root=tmp_path, state=state, runner=runner,
                             poster=_poster(calls=calls), sudo=False)
    assert (state / PARENT_RECORD).exists()
    assert calls == []
    assert runner.calls == []


def test_revocation_transaction_rolls_back_every_authority(tmp_path):
    store = grants._store()
    grant = grants.issue("parent")
    store.add_device("old", "old", "hash")
    with store._conn() as db:
        db.execute("CREATE TRIGGER refuse_revoke BEFORE UPDATE ON devices "
                   "BEGIN SELECT RAISE(ABORT, 'injected failure'); END")
    with pytest.raises(Exception, match="injected failure"):
        store.revoke_parent_authority()
    assert grants.authenticate(grant) == "parent"
    assert store.device("old")["revoked_at"] is None


def test_the_grants_go_even_when_the_parent_cannot_be_reached(tmp_path):
    """The one step that ends real authority is local and unconditional. A
    parent that is off must not be able to keep a machine attached."""
    token = grants.issue("http://studio.local:9090")
    _record(tmp_path / "state")
    result = detach_parent.detach(root=tmp_path, state=tmp_path / "state",
                                  runner=_Runner(), poster=_poster(0), sudo=False)
    assert grants.authenticate(token) is None
    assert _step(result, "parent-forget")["ok"] is False
    assert result["detached"] is True


def test_it_tells_the_parent_to_forget_the_tile_then_revoke_the_credential(
        tmp_path):
    """Order matters: the revoke is a self-revoke that kills the token both
    calls present, so a revoke-first detach would leave the tile forever."""
    calls = []
    _record(tmp_path / "state")
    detach_parent.detach(host_key="this-mac", root=tmp_path,
                         state=tmp_path / "state", runner=_Runner(),
                         poster=_poster(200, calls), sudo=False)
    assert [u for u, _ in calls] == [
        "http://studio.local:9090/api/jremote/v1/hosts/this-mac/forget",
        "http://studio.local:9090/api/jremote/v1/devices/dev1/revoke",
    ]
    # Authenticated with the credential the parent itself issued at attach.
    assert {t for _, t in calls} == {"jr1.dev1.secret"}


def test_a_parent_that_refuses_is_reported_with_the_manual_fix(tmp_path):
    _record(tmp_path / "state")
    result = detach_parent.detach(host_key="this-mac", root=tmp_path,
                                  state=tmp_path / "state", runner=_Runner(),
                                  poster=_poster(404), sudo=False)
    note = _step(result, "parent-forget")["note"]
    assert "by hand" in note and "404" in note


def test_no_parent_record_says_so_rather_than_silently_skipping(tmp_path):
    result = detach_parent.detach(root=tmp_path, state=tmp_path / "state",
                                  runner=_Runner(), poster=_poster(), sudo=False)
    assert _step(result, "parent")["ok"] is False
    assert "by hand" in _step(result, "parent")["note"]


def test_it_boots_out_both_leaf_daemons_and_deletes_what_the_installer_wrote(
        tmp_path):
    _leaf_installed(tmp_path)
    runner = _Runner()
    result = detach_parent.detach(root=tmp_path, state=tmp_path / "state",
                                  runner=runner, poster=_poster(), sudo=False)
    booted = [c for c in runner.calls if "bootout" in c]
    assert [c[-1] for c in booted] == ["system/com.jremote.leaf",
                                       "system/com.jremote.leaf-watch"]
    for raw in detach_parent.LEAF_PATHS:
        assert not (tmp_path / raw.lstrip("/")).exists(), raw
    assert _step(result, "tunnel-files")["ok"] is True
    assert result["detached"] is True


def test_an_unreadable_root_only_dir_routes_to_the_sudo_retry(tmp_path):
    """/etc/wireguard is 700 root on a real leaf, so stat from the enrolled
    account answers EACCES — which Path.exists() re-raises rather than
    swallows. The first live shell_detach run crashed exactly here; an
    unreadable path must reach the privileged removal, not the traceback."""
    guarded = tmp_path / "etc/wireguard"
    guarded.mkdir(parents=True)
    (guarded / "jrleaf.conf").write_text("[Interface]\n")
    guarded.chmod(0o000)
    runner = _Runner()
    try:
        result = detach_parent.detach(root=tmp_path, state=tmp_path / "state",
                                      runner=runner, poster=_poster(), sudo=False)
    finally:
        guarded.chmod(0o755)
    assert _step(result, "tunnel-files")["ok"] is True
    assert any("jrleaf.conf" in " ".join(map(str, argv)) and "/bin/rm" in argv
               for argv in runner.calls)


def test_a_daemon_that_was_already_down_is_not_a_failure(tmp_path):
    """`launchctl bootout` answers 3 for "no such process" — which is the state
    this is trying to reach, not a failure to reach it."""
    _leaf_installed(tmp_path)

    class Down(_Runner):
        def __call__(self, argv, **kw):
            self.calls.append(argv)
            return type("P", (), {"returncode": 3, "stdout": "",
                                  "stderr": "No such process"})()

    result = detach_parent.detach(root=tmp_path, state=tmp_path / "state",
                                  runner=Down(), poster=_poster(), sudo=False)
    assert _step(result, "bootout:com.jremote.leaf")["ok"] is True
    assert result["detached"] is True


def test_keep_tunnel_revokes_the_grants_and_leaves_the_mesh_up(tmp_path):
    """A machine that should stay on the mesh but stop being administered from
    it — the mesh is a transport, delegation is an authority."""
    token = grants.issue("parent")
    _leaf_installed(tmp_path)
    result = detach_parent.detach(root=tmp_path, state=tmp_path / "state",
                                  keep_tunnel=True, runner=_Runner(),
                                  poster=_poster(), sudo=False)
    assert grants.authenticate(token) is None
    assert (tmp_path / "etc/wireguard/jrleaf.conf").exists()
    assert result["detached"] is False
    assert "still on the parent's mesh" in _step(result, "tunnel")["note"]


def test_local_only_makes_no_network_call_at_all(tmp_path):
    calls = []
    _record(tmp_path / "state")
    detach_parent.detach(host_key="this-mac", root=tmp_path,
                         state=tmp_path / "state", tell_parent=False,
                         runner=_Runner(), poster=_poster(200, calls),
                         sudo=False)
    assert calls == []


def test_the_parent_record_and_bundle_are_dropped_last(tmp_path):
    """Last because `parent.json` holds the token the parent calls spend — a
    detach that deleted it first could not tell the parent anything."""
    state = tmp_path / "state"
    _record(state)
    (state / "leaf-bundle").mkdir(parents=True, exist_ok=True)
    (state / "leaf-bundle" / "jrleaf.conf").write_text("x")
    calls = []
    result = detach_parent.detach(host_key="this-mac", root=tmp_path,
                                  state=state, runner=_Runner(),
                                  poster=_poster(200, calls), sudo=False)
    assert calls, "the parent was told before the record went"
    assert not (state / PARENT_RECORD).exists()
    assert not (state / "leaf-bundle").exists()
    assert _step(result, "local-record")["ok"] is True


def test_detaching_a_machine_that_was_never_attached_is_clean_not_an_error(
        tmp_path):
    """Idempotence. Running detach twice, or on a host that never joined
    anything, has to be a no-op that says so."""
    result = detach_parent.detach(root=tmp_path, state=tmp_path / "state",
                                  runner=_Runner(), poster=_poster(), sudo=False)
    assert result["detached"] is True
    assert "no grants were outstanding" in _step(result, "grants")["note"]
