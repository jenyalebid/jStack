"""Release trust, durable jobs, authority, and restart recovery contracts."""
import base64
import hashlib
import io
import json
import tarfile
import time
from pathlib import Path

import httpx
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi.testclient import TestClient

from jstack_host import devices, hostenv, managed_access, mode
from jstack_host import attach_parent, fleet_updates as fleet, release_manifest as releases
from jstack_host import update_routes, store as stores
from jstack_host.update_supervisor import Supervisor, atomic_json


@pytest.fixture
def release():
    key = Ed25519PrivateKey.generate()
    components = {name: {"file": name + ".zip", "sha256": hashlib.sha256(b"artifact").hexdigest(),
                         "bytes": 8, "version": "1"} for name in releases.COMPONENTS}
    artifacts = hashlib.sha256(releases.canonical(components)).hexdigest()
    manifest = {"schema": 1, "release": "test-1", "components": components,
                "sources": {"stack": "a" * 40, "client": "b" * 40},
                "compatibility": {"protocol": 1, "rollback": True, "platform": "macos",
                                  "architecture": "arm64", "minimum_os": "13.0"},
                "receipts": {name: {"result": "passed", "skipped": 0, "artifacts": artifacts,
                                    "evidence_sha256": "c" * 64} for name in releases.RECEIPTS}}
    envelope = releases.sign(manifest, key.private_bytes_raw())
    return key, base64.b64encode(key.public_key().public_bytes_raw()).decode(), envelope


@pytest.mark.parametrize("corrupt_upload", [False, True])
@pytest.mark.parametrize("existing", [None, "draft", "published"])
def test_publication_checks_uploaded_bytes_before_exposing_release(tmp_path, release, monkeypatch, corrupt_upload, existing):
    import shutil
    import subprocess
    from jstack_host import release_channel, update_macos
    _, public, envelope = release
    atomic_json(tmp_path / "manifest.json", envelope)
    for item in envelope["manifest"]["components"].values():
        (tmp_path / item["file"]).write_bytes(b"artifact")
    monkeypatch.setattr(release_channel.subprocess, "run", lambda *a, **k:
                        subprocess.CompletedProcess(a[0], 0,
                            json.dumps({"draft": existing == "draft", "assets": [{"name": "manifest.json"}]}), "")
                        if existing else subprocess.CompletedProcess(a[0], 1, "", "HTTP 404"))
    calls = []
    def command(argv, **kwargs):
        calls.append(argv)
        if argv[1:3] == ["release", "download"]:
            from pathlib import Path
            name = argv[argv.index("--pattern") + 1]
            target = Path(argv[argv.index("--dir") + 1]) / name
            shutil.copy2(tmp_path / name, target)
            if corrupt_upload and name == "client.zip":
                target.write_bytes(b"corrupt")
        return ""
    monkeypatch.setattr(update_macos, "command", command)
    if corrupt_upload:
        with pytest.raises(releases.ReleaseError):
            release_channel.publish(tmp_path, "example/stack", public)
        assert not any(c[1:3] == ["release", "edit"] for c in calls)
    else:
        release_channel.publish(tmp_path, "example/stack", public)
        if existing == "published":
            assert all(c[1:3] == ["release", "download"] for c in calls)
        else:
            stack_calls = [c for c in calls if c[3] == "stack-release-test-1"]
            assert stack_calls[-1][1:3] == ["release", "edit"]
        if existing is None:
            assert calls[0][calls[0].index("--target") + 1] == "a" * 40


def test_signature_rejects_payload_and_trust_key_substitution(release):
    key, public, envelope = release
    assert releases.verify(envelope, public)["release"] == "test-1"
    wrong = Ed25519PrivateKey.generate()
    envelope["public_key"] = base64.b64encode(wrong.public_key().public_bytes_raw()).decode()
    assert releases.verify(envelope, public)["release"] == "test-1"
    envelope["manifest"]["release"] = "tampered"
    with pytest.raises(releases.ReleaseError, match="signature"):
        releases.verify(envelope, public)
    with pytest.raises(releases.ReleaseError, match="signature"):
        releases.verify(releases.sign(envelope["manifest"], wrong.private_bytes_raw()), public)


@pytest.mark.parametrize("invalid_artifact", [False, True])
def test_public_channel_advances_only_after_complete_verified_download(tmp_path, release, invalid_artifact):
    from jstack_host import release_channel
    root = tmp_path / "updates"
    feed = tmp_path / "feed"
    previous = releases.sign({**release[2]["manifest"], "release": "previous"}, release[0].private_bytes_raw())
    atomic_json(feed / "latest.json", previous)
    tag = release_channel.TAG_PREFIX + "test-1"
    calls = []

    def transport(request):
        calls.append(str(request.url))
        if request.url.host == "api.github.com":
            return httpx.Response(200, json=[
                {"draft": True, "prerelease": False, "tag_name": tag + "-draft"},
                {"draft": False, "prerelease": True, "tag_name": tag + "-preview"},
                {"draft": False, "prerelease": False, "tag_name": "mac-71"},
                {"draft": False, "prerelease": False, "tag_name": tag}])
        if request.url.path.endswith("manifest.json"):
            return httpx.Response(200, json=release[2])
        return httpx.Response(200, content=b"tampered" if invalid_artifact else b"artifact")

    config = {"github_repo": "example/stack", "feed_dir": str(feed), "public_key": release[1]}
    with httpx.Client(transport=httpx.MockTransport(transport)) as client:
        if invalid_artifact:
            with pytest.raises(releases.ReleaseError):
                release_channel.refresh(root, config, client=client, now=1000)
            assert json.loads((feed / "latest.json").read_text()) == previous
        else:
            release_channel.refresh(root, config, client=client, now=1000)
            assert json.loads((feed / "latest.json").read_text()) == release[2]
            for component in release[2]["manifest"]["components"].values():
                assert (feed / "test-1" / component["file"]).read_bytes() == b"artifact"
        previous_calls = len(calls)
        release_channel.refresh(root, config, client=client, now=1001)
        assert len(calls) == previous_calls


@pytest.mark.parametrize("excluded", ["managed", "candidate_test", "parent_record"])
def test_public_channel_never_overrides_parent_or_candidate_feed(tmp_path, excluded):
    from jstack_host import release_channel
    config = {"github_repo": "example/stack"}
    if excluded == "parent_record":
        (tmp_path / "parent.json").write_text("{}")
    else:
        config[excluded] = True
    release_channel.refresh(tmp_path / "updates", config)
    assert not (tmp_path / "updates" / "channel.json").exists()


@pytest.mark.parametrize("mutation", ["missing", "skipped", "stale", "wrong_digest", "failed"])
def test_promotion_requires_exact_artifact_receipts(release, mutation):
    _, _, envelope = release
    manifest = envelope["manifest"]
    receipt = manifest["receipts"]["off_network"]
    if mutation == "missing":
        del manifest["receipts"]["off_network"]
    elif mutation == "skipped":
        receipt["skipped"] = 1
    elif mutation == "stale":
        manifest["components"]["stack"]["version"] = "2"
    elif mutation == "wrong_digest":
        receipt["evidence_sha256"] = ""
    else:
        receipt["result"] = "failed"
    with pytest.raises(releases.ReleaseError, match="receipt"):
        releases.validate(manifest)


@pytest.mark.parametrize("filename", ["../escape", "/absolute", "a/b", "", ".", "..", "a\\b"])
def test_artifact_paths_are_bare_safe_names(release, filename):
    release[2]["manifest"]["components"]["stack"]["file"] = filename
    with pytest.raises(releases.ReleaseError):
        releases.validate(release[2]["manifest"])


def test_job_survives_reopen_duplicate_delivery_and_rejects_overlap(tmp_path, release):
    path = tmp_path / "jobs.sqlite"
    job = fleet.FleetStore(path).queue("leaf", "credential", release[2], "click-one")
    reopened = fleet.FleetStore(path)
    assert reopened.queue("leaf", "credential", release[2], "click-one")["id"] == job["id"]
    assert reopened.queue("leaf", "credential", release[2], "click-two")["id"] == job["id"]
    release[2]["manifest"]["release"] = "test-2"
    with pytest.raises(releases.ReleaseError):
        reopened.queue("leaf", "credential", release[2], "click-two")
    with pytest.raises(releases.ReleaseError):
        reopened.transition(job["id"], "other-leaf", "downloading")
    with pytest.raises(releases.ReleaseError):
        reopened.transition(job["id"], "leaf", "current")


def test_current_requires_verification_and_expired_inventory_is_unknown(tmp_path, release):
    store = fleet.FleetStore(tmp_path / "jobs.sqlite")
    job = store.queue("leaf", "cred", release[2], "click")
    for state in ("downloading", "applying", "verifying"):
        store.transition(job["id"], "leaf", state)
    with pytest.raises(releases.ReleaseError, match="verification"):
        store.transition(job["id"], "leaf", "current")
    store.transition(job["id"], "leaf", "current", verified=True)
    store.report("leaf", {"release": "test-1", "verified": True, "supervisor": 1})
    assert store.inventory("leaf", "Office", "test-1")["state"] == "current"
    assert store.inventory("leaf", "Office", "test-2")["state"] == "available"
    with store.connection() as db:
        db.execute("UPDATE reports SET seen=?", (time.time() - 100,))
    assert store.inventory("leaf", "Office", "test-1")["state"] == "unknown/offline"


def test_fresh_install_at_the_desired_release_is_current_not_available(tmp_path):
    # No job ever ran and nothing was verified: the installer put the release
    # there. Running exactly the desired release means there is no update to
    # offer — the menu read "Update Available" on every fresh install
    # (proven 2026-09-22, guest vfy-full-instances, release ffe85dc9).
    store = fleet.FleetStore(tmp_path / "jobs.sqlite")
    store.report("leaf", {"release": "test-1", "verified": False, "supervisor": 1})
    assert store.inventory("leaf", "Office", "test-1")["state"] == "current"
    assert store.inventory("leaf", "Office", "test-2")["state"] == "available"
    assert store.inventory("leaf", "Office", None)["state"] == "not_published"


def test_parent_accepts_a_leaf_rollback_after_restart_during_download(tmp_path, release):
    store = fleet.FleetStore(tmp_path / "jobs.sqlite")
    job = store.queue("leaf", "cred", release[2], "click")
    store.transition(job["id"], "leaf", "downloading")

    recovered = store.transition(
        job["id"], "leaf", "rolled_back", "authority lost during application")

    assert recovered["state"] == "rolled_back"


@pytest.fixture
def rig(tmp_path, monkeypatch, app, release):
    state = tmp_path / "state"
    monkeypatch.setattr(hostenv, "state_dir", lambda: state)
    monkeypatch.setattr(hostenv, "host_id", lambda: "hub-main")
    monkeypatch.setattr(hostenv, "host_name", lambda: "Home")
    monkeypatch.setattr(attach_parent, "parent_record", lambda: {})
    monkeypatch.setattr(mode, "is_managed", lambda: False)
    monkeypatch.setattr(mode, "is_hub", lambda: True)
    monkeypatch.setattr(fleet, "offer", lambda: release[2])
    store = stores.get_store()
    monkeypatch.setattr(devices, "_store", lambda: store)
    internal = devices.internal_token()
    console = TestClient(app, client=("127.0.0.1", 4000),
                         headers={"Authorization": "Bearer " + internal})
    phone, token = devices.mint("Phone")
    remote = TestClient(app, client=("10.66.0.8", 4001),
                        headers={"Authorization": "Bearer " + token})
    return store, console, remote


def test_phone_cannot_queue_updates_even_from_loopback(rig):
    _, console, remote = rig
    body = {"target": "all", "request_id": "click"}
    assert remote.post("/api/jremote/v1/updates/queue", json=body).status_code == 403
    assert console.post("/api/jremote/v1/updates/queue", headers=remote.headers,
                        json=body).status_code == 403
    assert console.post("/api/jremote/v1/updates/queue", json=body).status_code == 200
    assert remote.post("/api/jremote/v1/managed/updates/heartbeat", json={}).status_code == 403


def test_offline_job_delivered_on_reconnect_and_revocation_refuses_it(rig):
    store, console, remote = rig
    row, token = devices.mint("Office")
    store.upsert_host("leaf-one", "Office", "10.66.0.9")
    store.bind_host_device("leaf-one", row["id"])
    store.set_host_visibility("leaf-one", sees_home=False, sees_leaves=False)
    queued = console.post("/api/jremote/v1/updates/queue",
                          json={"target": "leaf-one", "request_id": "click"})
    assert queued.status_code == 200, queued.text
    inventory = console.get("/api/jremote/v1/updates/inventory").json()
    assert inventory["machines"][1]["state"] == "pending/offline"
    headers = {"Authorization": "Bearer " + token}
    path = "/api/jremote/v1/managed/updates/heartbeat"
    response = remote.post(path, headers=headers, json={"observation": {"supervisor": 1}})
    assert response.status_code == 200, response.text
    assert response.json()["job"]["id"] == queued.json()["jobs"][0]["id"]
    assert remote.post("/api/jremote/v1/managed/updates/check", headers=headers).status_code == 200
    devices.revoke(row["id"])
    assert remote.post(path, headers=headers, json={}).status_code == 401


def test_job_cannot_be_claimed_by_another_machine(rig):
    store, console, remote = rig
    tokens = []
    for key in ("leaf-one", "leaf-two"):
        row, token = devices.mint(key)
        store.upsert_host(key, key, "10.66.0.9")
        store.bind_host_device(key, row["id"])
        tokens.append(token)
    job = console.post("/api/jremote/v1/updates/queue",
                       json={"target": "leaf-one", "request_id": "click"}).json()["jobs"][0]
    response = remote.post("/api/jremote/v1/managed/updates/heartbeat",
                           headers={"Authorization": "Bearer " + tokens[1]},
                           json={"job_id": job["id"], "state": "current",
                                 "observation": {"release": "test-1", "verified": True}})
    assert fleet.FleetStore().latest("leaf-one")["state"] == "pending"
    assert response.json().get("job") is None


class Backend:
    def __init__(self):
        self.events = []
        self.healthy = True

    def compatible(self, manifest):
        self.events.append("compatible")

    def stage(self, manifest, directory):
        self.events.append("stage")
        return {"previous": "old"}

    def apply(self, job):
        assert job["state"] == "applying" and job["transaction"]["previous"] == "old"
        self.events.append("apply")

    def rollback(self, job):
        self.events.append("rollback")

    def finalize(self, job):
        self.events.append("finalize")

    def verify(self, job):
        return self.healthy

    def observe(self, job):
        return {"release": job.get("release"), "verified": bool(job.get("verified"))}


def supervisor(tmp_path, release, backend=None):
    root = tmp_path / "updates"
    root.mkdir(exist_ok=True)
    token = tmp_path / "token"
    token.write_text("test-token")
    configuration = {"local_url": "http://hub", "token_path": str(token), "public_key": release[1]}
    job = {"id": "job-1", "state": "pending", "release": "test-1", "envelope": release[2]}

    def transport(request):
        if request.method == "GET":
            return httpx.Response(200, content=b"artifact")
        body = json.loads(request.content)
        if body.get("state"):
            job["state"] = body["state"]
        if body.get("observation", {}).get("verified"):
            job["state"] = "current"
        return httpx.Response(200, json={"job": job})
    client = httpx.Client(transport=httpx.MockTransport(transport))
    return Supervisor(root, configuration, backend or Backend(), client), job


def test_supervisor_stages_before_apply_then_requires_hub_confirmation(tmp_path, release):
    daemon, job = supervisor(tmp_path, release)
    daemon.tick()
    assert daemon.backend.events == ["compatible", "stage", "apply"]
    assert daemon.current["state"] == "verifying"
    daemon.tick()
    assert daemon.current["state"] == "current"
    assert daemon.current["finalized"] is True
    assert daemon.backend.events[-1] == "finalize"
    daemon.tick()
    assert daemon.backend.events.count("apply") == 1
    assert daemon.backend.events.count("finalize") == 1


def test_crash_mid_apply_recovers_from_disk_before_accepting_work(tmp_path, release):
    daemon, job = supervisor(tmp_path, release)
    daemon.current = {**job, "state": "applying", "transaction": {"previous": "old"}}
    daemon.save()
    restarted = Supervisor(daemon.root, daemon.config, Backend(), daemon.client)
    restarted.tick()
    assert restarted.backend.events == ["rollback"]
    assert restarted.current["state"] == "rolled_back"


@pytest.mark.parametrize("status,expected", [
    ("pending", "applying"), ("applied", "verifying"), ("rolled_back", "rolled_back")])
def test_independent_owner_recovery_controls_restart_outcome(tmp_path, release, status, expected):
    backend = Backend()
    backend.healthy = False
    backend.recovery_status = lambda job: status
    daemon, job = supervisor(tmp_path, release, backend)
    daemon.current = {**job, "state": "applying", "transaction": {"native": True}}
    daemon.save()
    daemon.tick()
    assert daemon.current["state"] == expected
    assert ("rollback" in backend.events) is False


def test_root_watchdog_marker_settles_a_stalled_apply_without_a_second_swap(tmp_path, release):
    backend = Backend()
    backend.recovery_status = lambda job: "applied"
    daemon, job = supervisor(tmp_path, release, backend)
    daemon.current = {**job, "state": "applying", "transaction": {"native": True}}
    daemon.save()
    (daemon.root / "recovery.json").write_text(json.dumps(
        {"schema": 1, "restored": "Hub.app.previous-stage"}))
    daemon.tick()
    assert daemon.current["state"] == "rolled_back"
    assert daemon.current["detail"] == "root watchdog restored the retained hub backup"
    assert backend.events == ["rollback"]


def test_a_past_recovery_marker_never_settles_a_new_stall(tmp_path, release):
    import os
    backend = Backend()
    backend.healthy = False
    backend.recovery_status = lambda job: "applied"
    daemon, job = supervisor(tmp_path, release, backend)
    marker = daemon.root / "recovery.json"
    marker.write_text("{}")
    os.utime(marker, (time.time() - 3600,) * 2)
    daemon.current = {**job, "state": "applying", "transaction": {"native": True}}
    daemon.save()
    daemon.tick()
    assert daemon.current["state"] == "verifying"
    assert "rollback" not in backend.events


def test_verification_waits_while_independent_owner_rolls_back(tmp_path, release):
    backend = Backend()
    backend.recovery_status = lambda job: "rolling_back"
    daemon, job = supervisor(tmp_path, release, backend)
    daemon.current = {**job, "state": "verifying", "verify_started": time.time() - 500,
                      "transaction": {"native": True}}
    daemon.save()
    daemon.tick()
    assert daemon.current["state"] == "verifying"
    assert "rollback" not in backend.events


def test_failed_verification_rolls_back(tmp_path, release):
    daemon, job = supervisor(tmp_path, release)
    daemon.tick()
    daemon.backend.healthy = False
    daemon.save(verify_started=time.time() - 150)
    daemon.tick()
    assert daemon.current["state"] == "rolled_back"


def test_tampered_download_never_reaches_apply(tmp_path, release):
    daemon, job = supervisor(tmp_path, release)
    original = daemon.client

    def transport(request):
        if request.method == "GET":
            return httpx.Response(200, content=b"TAMPERED")
        return original.send(request)
    daemon.client = httpx.Client(transport=httpx.MockTransport(transport))
    with pytest.raises(releases.ReleaseError):
        daemon.tick()
    assert "apply" not in daemon.backend.events
    assert daemon.current["state"] == "failed"


@pytest.mark.parametrize("kind", [tarfile.SYMTYPE, tarfile.LNKTYPE, tarfile.CHRTYPE])
def test_stack_archive_rejects_links_and_devices(tmp_path, kind):
    from jstack_host.update_macos import safe_tar
    archive = tmp_path / "bad.tar"
    with tarfile.open(archive, "w") as bundle:
        member = tarfile.TarInfo("link")
        member.type, member.linkname = kind, "../../outside"
        bundle.addfile(member)
    with pytest.raises(releases.ReleaseError):
        safe_tar(archive, tmp_path / "unpacked")


def test_verification_timeout_recovers_without_network(tmp_path, release):
    daemon, job = supervisor(tmp_path, release)
    daemon.tick()
    daemon.save(verify_started=time.time() - 181)
    def offline(request):
        raise httpx.ConnectError("offline", request=request)
    daemon.client = httpx.Client(transport=httpx.MockTransport(offline))
    with pytest.raises(httpx.ConnectError):
        daemon.tick()
    assert daemon.backend.events[-1] == "rollback"
    assert daemon.current["state"] == "rolled_back"


def test_restart_adopts_hub_confirmation_before_local_commit(tmp_path, release):
    daemon, job = supervisor(tmp_path, release)
    daemon.tick()
    daemon.save(verified=True)
    restarted = Supervisor(daemon.root, daemon.config, daemon.backend, daemon.client)
    restarted.tick()
    assert restarted.current["state"] == "current"
    assert restarted.current["finalized"] is True
    assert restarted.backend.events.count("apply") == 1


def test_confirmed_transaction_removes_temporary_app_backups(tmp_path):
    from jstack_host.update_macos import MacBackend
    target = tmp_path / "Client.app"
    target.mkdir()
    backup = tmp_path / "Client.app.previous-release-stage-123"
    backup.mkdir()
    (backup / "old").write_text("old")
    job = {"transaction": {"apps": {"client": {
        "target": str(target), "backup": str(backup)}}}}
    backend = MacBackend(tmp_path, {})
    backend.finalize(job)
    assert target.is_dir()
    assert not backup.exists()
    backend.finalize(job)


def test_finalize_refuses_a_path_outside_the_updaters_backup_shape(tmp_path):
    from jstack_host.update_macos import MacBackend
    target = tmp_path / "Client.app"
    target.mkdir()
    unrelated = tmp_path / "Keep.app"
    unrelated.mkdir()
    job = {"transaction": {"apps": {"client": {
        "target": str(target), "backup": str(unrelated)}}}}
    with pytest.raises(releases.ReleaseError, match="unexpected recovery bundle"):
        MacBackend(tmp_path, {}).finalize(job)
    assert unrelated.is_dir()


def test_completed_job_accepts_repeated_current_heartbeats(rig):
    _, console, remote = rig
    job = console.post("/api/jremote/v1/updates/queue",
                       json={"target": "self", "request_id": "click"}).json()["jobs"][0]
    store = fleet.FleetStore()
    for state in ("downloading", "applying", "verifying"):
        store.transition(job["id"], "hub-main", state)
    store.transition(job["id"], "hub-main", "current", verified=True)
    for _ in range(2):
        result = console.post("/api/jremote/v1/updates/heartbeat",
                              json={"job_id": job["id"], "state": "current"})
        assert result.status_code == 200, result.text
        assert result.json()["job"]["state"] == "current"


def test_revocation_cancels_pending_inventory_job(rig):
    store, console, remote = rig
    row, token = devices.mint("Office")
    store.upsert_host("leaf-one", "Office", "10.66.0.9")
    store.bind_host_device("leaf-one", row["id"])
    response = console.post("/api/jremote/v1/updates/queue",
                            json={"target": "leaf-one", "request_id": "click"})
    assert response.status_code == 200
    devices.revoke(row["id"])
    assert fleet.FleetStore().inventory("leaf-one", "Office", "test-1")["state"] == "cancelled"


def test_atomic_replacement_preserves_executable_mode_and_symlink_target(tmp_path):
    from jstack_host.update_macos import atomic_bytes
    original, launcher = tmp_path / "original", tmp_path / "launcher"
    original.write_bytes(b"old")
    original.chmod(0o755)
    launcher.symlink_to(original)
    atomic_bytes(launcher, b"new")
    assert not launcher.is_symlink()
    assert launcher.read_bytes() == b"new"
    assert original.read_bytes() == b"old"
    assert launcher.stat().st_mode & 0o777 == 0o755


def test_plugin_recovery_failure_does_not_strand_host_and_apps(tmp_path, monkeypatch):
    from jstack_host import update_macos, update_plugins
    events = []
    backend = update_macos.MacBackend(tmp_path, {})
    monkeypatch.setattr(backend, "_unload", lambda kind: events.append("stop-" + kind))
    monkeypatch.setattr(backend, "_load", lambda kind: events.append("start-" + kind))
    monkeypatch.setattr(update_macos, "stop_app", lambda path: None)
    def broken(*args):
        raise releases.ReleaseError("provider unavailable")
    monkeypatch.setattr(update_plugins, "rollback", broken)
    target, backup = tmp_path / "Client.app", tmp_path / "Client.previous"
    target.mkdir()
    (target / "version").write_text("new")
    backup.mkdir()
    (backup / "version").write_text("old")
    original, plist = tmp_path / "original.plist", tmp_path / "host.plist"
    original.write_bytes(b"old service")
    plist.write_bytes(b"new service")
    job = {"id": "test", "transaction": {"providers": [], "stack": str(tmp_path),
           "apps": {"client": {"target": str(target), "backup": str(backup),
                               "existed": True, "was_running": False}},
           "plists": {"host": {"target": str(plist), "original": str(original)}}}}
    with pytest.raises(releases.ReleaseError, match="host/apps restored"):
        backend.rollback(job)
    assert (target / "version").read_text() == "old"
    assert plist.read_bytes() == b"old service"
    assert events[-2:] == ["start-host", "start-menubar"]
    # Recovery retries after the provider becomes available without moving
    # the already-restored app into a failed bundle a second time.
    monkeypatch.setattr(update_plugins, "rollback", lambda *args: None)
    backend.rollback(job)
    assert (target / "version").read_text() == "old"


def test_promote_checks_evidence_bytes_and_preserves_prior_feed(tmp_path, release):
    from jstack_host.publish_release import promote
    key, public, envelope = release
    candidate, evidence, feed = (tmp_path / name for name in ("candidate", "evidence", "feed"))
    for directory in (candidate, evidence, feed):
        directory.mkdir()
    for item in envelope["manifest"]["components"].values():
        (candidate / item["file"]).write_bytes(b"artifact")
    atomic_json(candidate / "candidate.json", envelope)
    atomic_json(feed / "latest.json", {"prior": "untouched"})
    for name, receipt in envelope["manifest"]["receipts"].items():
        log = evidence / (name + ".log")
        log.write_text("synthetic UNIT TEST evidence, never production acceptance")
        atomic_json(evidence / (name + ".json"), {**receipt, "evidence_sha256": releases.digest(log)})
    (evidence / "off_network.log").write_text("changed")
    with pytest.raises(releases.ReleaseError, match="evidence"):
        promote(candidate, evidence, feed, key.private_bytes_raw())
    assert json.loads((feed / "latest.json").read_text()) == {"prior": "untouched"}


@pytest.mark.parametrize("state,verified,changes", [("verifying", True, False),
                                                 ("current", False, False), ("current", True, True)])
def test_updater_runtime_moves_only_after_confirmed_transaction(tmp_path, state, verified, changes):
    from jstack_host.update_macos import MacBackend
    stack = tmp_path / "release/stack"
    (stack / "host/jstack_host").mkdir(parents=True)
    (stack / "host/jstack_host/update_dispatcher.py").write_text("# test fixture")
    configuration = {"dispatcher": str(tmp_path / "stable-dispatcher"), "runtime_imports": ["old"]}
    atomic_json(tmp_path / "config.json", configuration)
    backend = MacBackend(tmp_path, configuration)
    job = {"state": state, "verified": verified,
           "transaction": {"stack": str(stack), "stage": str(tmp_path / "release")}}
    assert backend.activate_runtime(job) is changes
    updated = json.loads((tmp_path / "config.json").read_text())
    assert updated["dispatcher"] == configuration["dispatcher"]
    assert (updated["runtime_imports"] != ["old"]) is changes


def test_launchd_replacement_waits_for_service_removal(tmp_path, monkeypatch):
    import subprocess
    from jstack_host import update_macos
    calls = []
    remaining = iter([0, 0, 113])
    def run(argv, **kwargs):
        calls.append(argv[1])
        return subprocess.CompletedProcess(argv, next(remaining) if argv[1] == "print" else 0)
    monkeypatch.setattr(update_macos.subprocess, "run", run)
    monkeypatch.setattr(update_macos.time, "sleep", lambda _: None)
    update_macos.MacBackend(tmp_path, {"host_label": "lab-host"})._unload("host")
    assert calls == ["bootout", "print", "print", "print"]


def test_inventory_observes_updater_source_and_requires_live_menubar(tmp_path, monkeypatch):
    from jstack_host import update_macos, update_plugins, sourcestamp
    token = tmp_path / "token"
    token.write_text("test")
    config = {"client_path": "Client", "menubar_path": "Menu", "token_path": str(token),
              "local_url": "http://test"}
    monkeypatch.setattr(update_macos, "bundle_info", lambda _: {"CFBundleVersion": "1"})
    monkeypatch.setattr(update_plugins, "discover", lambda: [])
    monkeypatch.setattr(update_plugins, "observed", lambda _: {})
    monkeypatch.setattr(sourcestamp, "capture", lambda: {"sha": "actual-updater"})
    transport = httpx.MockTransport(lambda r: httpx.Response(200, json={"source": {
        "release": "r1", "dirty": False}}))
    real_client = httpx.Client
    monkeypatch.setattr(update_macos.httpx, "Client", lambda **kwargs: real_client(transport=transport))
    job = {"verified": True, "state": "current", "release": "r1", "envelope": {"manifest": {
        "components": {"client": {"version": "1"}, "menubar": {"version": "1"}}}}}
    backend = update_macos.MacBackend(tmp_path, config)
    monkeypatch.setattr(update_macos, "running", lambda _: [12])
    assert backend.observe(job)["verified"] is True
    assert backend.observe(job)["updater_source"] == {"sha": "actual-updater"}
    monkeypatch.setattr(update_macos, "running", lambda _: [])
    assert backend.observe(job)["verified"] is False


def test_running_observes_exec_after_cached_process_scan(tmp_path):
    """A launchd-style same-PID exec must not stay cached as its launcher."""
    import plistlib
    import subprocess
    import time
    import psutil
    from jstack_host.update_macos import running

    app = tmp_path / "Fixture.app"
    # Keep Apple's platform binary in place; copying it would test signature
    # enforcement instead of the process exec transition.
    executable = Path("/bin/sleep")
    (app / "Contents").mkdir(parents=True)
    (app / "Contents/Info.plist").write_bytes(plistlib.dumps({"CFBundleExecutable": str(executable)}))
    process = subprocess.Popen(["/bin/sh", "-c", 'read ready; exec "$1" 30',
                                "fixture-launcher", str(executable)], stdin=subprocess.PIPE)
    try:
        # Seed the precise stale cache that launch-time process scanning makes.
        assert process.pid in [p.pid for p in psutil.process_iter(["pid", "exe"])]
        assert process.pid not in running(app)
        process.stdin.write(b"go\n")
        process.stdin.flush()
        deadline = time.monotonic() + 5
        while psutil.Process(process.pid).exe() != str(executable):
            assert time.monotonic() < deadline, "fixture did not exec"
            time.sleep(0.01)
        assert process.pid in running(app)
    finally:
        process.terminate()
        process.wait(timeout=5)
        process.stdin.close()


def _channel_feed(release, offers, manifests):
    """A GitHub release list and the manifest each tag serves.

    `offers` is what the API lists, `manifests` maps tag -> signed envelope.
    A tag with no entry serves a 404, which is how a client-only release —
    one that publishes no stack manifest at all — looks from here.
    """
    def transport(request):
        if request.url.host == "api.github.com":
            return httpx.Response(200, json=offers)
        tag = str(request.url).split("/download/")[1].split("/")[0]
        if request.url.path.endswith("manifest.json"):
            envelope = manifests.get(tag)
            return httpx.Response(200, json=envelope) if envelope else httpx.Response(404)
        return httpx.Response(200, content=b"artifact")
    return httpx.MockTransport(transport)


def _signed(fixture, **fields):
    return releases.sign({**fixture[2]["manifest"], **fields}, fixture[0].private_bytes_raw())


def test_a_hub_on_stable_never_takes_a_branch_release(tmp_path, release):
    """The branch line is published as a prerelease, and stable's filter drops
    prereleases — so a side branch cannot reach a hub that did not ask for it
    even if it is the newest thing published."""
    from jstack_host import release_channel
    branch_tag = release_channel.TAG_PREFIX + "branch-1"
    stable_tag = release_channel.TAG_PREFIX + "test-1"
    offers = [{"draft": False, "prerelease": True, "tag_name": branch_tag},
              {"draft": False, "prerelease": False, "tag_name": stable_tag}]
    manifests = {
        branch_tag: _signed(release, release="branch-1", sequence=99,
                            channel={"name": "feature/x"}),
        stable_tag: _signed(release, release="test-1", sequence=5,
                            channel={"name": "stable"})}
    feed = tmp_path / "feed"
    config = {"github_repo": "example/stack", "feed_dir": str(feed),
              "public_key": release[1]}
    with httpx.Client(transport=_channel_feed(release, offers, manifests)) as client:
        release_channel.refresh(tmp_path / "updates", config, client=client, now=1000)
    assert json.loads((feed / "latest.json").read_text())["manifest"]["release"] == "test-1"


def test_a_hub_switched_to_a_branch_takes_that_branchs_release(tmp_path, release):
    """The whole of the switch: a channel name in this hub's config."""
    from jstack_host import release_channel
    branch_tag = release_channel.TAG_PREFIX + "branch-1"
    stable_tag = release_channel.TAG_PREFIX + "test-1"
    offers = [{"draft": False, "prerelease": True, "tag_name": branch_tag},
              {"draft": False, "prerelease": False, "tag_name": stable_tag}]
    manifests = {
        branch_tag: _signed(release, release="branch-1", sequence=99,
                            channel={"name": "feature/x"}),
        stable_tag: _signed(release, release="test-1", sequence=5,
                            channel={"name": "stable"})}
    feed = tmp_path / "feed"
    config = {"github_repo": "example/stack", "feed_dir": str(feed),
              "public_key": release[1], "channel": "feature/x"}
    with httpx.Client(transport=_channel_feed(release, offers, manifests)) as client:
        release_channel.refresh(tmp_path / "updates", config, client=client, now=1000)
    assert json.loads((feed / "latest.json").read_text())["manifest"]["release"] == "branch-1"


def test_the_channel_comes_from_the_signed_manifest_not_the_tag(tmp_path, release):
    """A tag is metadata anyone with push rights can write; the line a hub
    follows is a trust decision. A tag that says stable over a manifest that
    says otherwise is not an offer to a stable hub."""
    from jstack_host import release_channel
    liar = release_channel.TAG_PREFIX + "test-1"
    offers = [{"draft": False, "prerelease": False, "tag_name": liar}]
    manifests = {liar: _signed(release, release="test-1", sequence=99,
                               channel={"name": "feature/x"})}
    feed = tmp_path / "feed"
    config = {"github_repo": "example/stack", "feed_dir": str(feed),
              "public_key": release[1]}
    root = tmp_path / "updates"
    with httpx.Client(transport=_channel_feed(release, offers, manifests)) as client:
        release_channel.refresh(root, config, client=client, now=1000)
    assert not (feed / "latest.json").exists()
    assert json.loads((root / "channel.json").read_text())["status"] == "not_published"


def test_the_channel_refuses_to_walk_a_hub_backwards(tmp_path, release):
    """The guard the counter used to provide. It is a sequence now, because
    the identity is a hash and a date and neither of those orders."""
    from jstack_host import release_channel
    tag = release_channel.TAG_PREFIX + "older"
    offers = [{"draft": False, "prerelease": False, "tag_name": tag}]
    manifests = {tag: _signed(release, release="older", sequence=4,
                              channel={"name": "stable"})}
    feed = tmp_path / "feed"
    current = _signed(release, release="newer", sequence=9, channel={"name": "stable"})
    atomic_json(feed / "latest.json", current)
    config = {"github_repo": "example/stack", "feed_dir": str(feed),
              "public_key": release[1]}
    with httpx.Client(transport=_channel_feed(release, offers, manifests)) as client:
        with pytest.raises(releases.ReleaseError):
            release_channel.refresh(tmp_path / "updates", config, client=client, now=1000)
    assert json.loads((feed / "latest.json").read_text()) == current


def test_moving_to_a_branch_is_not_a_downgrade(tmp_path, release):
    """Two counts only compare along one line. A hub deliberately switched is
    changing which history it measures against, and its old count means
    nothing on the new one — so a branch whose sequence is lower still lands."""
    from jstack_host import release_channel
    tag = release_channel.TAG_PREFIX + "branch-1"
    offers = [{"draft": False, "prerelease": True, "tag_name": tag}]
    manifests = {tag: _signed(release, release="branch-1", sequence=2,
                              channel={"name": "feature/x"})}
    feed = tmp_path / "feed"
    atomic_json(feed / "latest.json",
                _signed(release, release="newer", sequence=9, channel={"name": "stable"}))
    config = {"github_repo": "example/stack", "feed_dir": str(feed),
              "public_key": release[1], "channel": "feature/x"}
    with httpx.Client(transport=_channel_feed(release, offers, manifests)) as client:
        release_channel.refresh(tmp_path / "updates", config, client=client, now=1000)
    assert json.loads((feed / "latest.json").read_text())["manifest"]["release"] == "branch-1"


def test_a_manifest_from_before_channels_reads_as_stable(tmp_path, release):
    """Every release already published came off main. If absent read as "no
    channel" instead of stable, every hub in the field would stop updating."""
    from jstack_host import release_channel
    tag = release_channel.TAG_PREFIX + "test-1"
    offers = [{"draft": False, "prerelease": False, "tag_name": tag}]
    manifests = {tag: release[2]}          # no channel name, no sequence
    feed = tmp_path / "feed"
    config = {"github_repo": "example/stack", "feed_dir": str(feed),
              "public_key": release[1]}
    with httpx.Client(transport=_channel_feed(release, offers, manifests)) as client:
        release_channel.refresh(tmp_path / "updates", config, client=client, now=1000)
    assert json.loads((feed / "latest.json").read_text()) == release[2]


@pytest.mark.parametrize("name", ["../etc", "-rf", "a b", "x" * 200])
def test_a_channel_name_that_is_not_a_branch_is_refused(name):
    """The name is pasted into a comparison against signed content and comes
    from a config file — bound it here, once, rather than at each use."""
    from jstack_host import release_channel
    with pytest.raises(releases.ReleaseError):
        release_channel.channel_name({"channel": name})


def test_no_channel_configured_is_the_stable_line():
    from jstack_host import release_channel
    assert release_channel.channel_name({}) == releases.STABLE_CHANNEL


def _channel_cli(state, *argv):
    import io
    import contextlib
    from jstack_host import cli
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = cli.main(["updates", "channel", *argv, "--state-dir", str(state)])
    return code, out.getvalue().strip(), err.getvalue().strip()


def test_switching_a_hub_to_a_branch_is_one_command(tmp_path, monkeypatch):
    """A hub is switched by naming a branch, not by editing JSON. The write
    goes to the same config `refresh` reads and a reinstall carries forward."""
    monkeypatch.delenv("JREMOTE_STATE_DIR", raising=False)
    config = tmp_path / "updates" / "config.json"
    atomic_json(config, {"github_repo": "example/stack"})
    assert _channel_cli(tmp_path)[:2] == (0, releases.STABLE_CHANNEL)
    assert _channel_cli(tmp_path, "feature/x")[:2] == (0, "feature/x")
    assert json.loads(config.read_text())["channel"] == "feature/x"
    assert json.loads(config.read_text())["github_repo"] == "example/stack"
    assert _channel_cli(tmp_path)[:2] == (0, "feature/x")
    assert _channel_cli(tmp_path, "stable")[:2] == (0, releases.STABLE_CHANNEL)


def test_a_mistyped_branch_name_is_a_sentence_not_a_traceback(tmp_path, monkeypatch):
    monkeypatch.delenv("JREMOTE_STATE_DIR", raising=False)
    atomic_json(tmp_path / "updates" / "config.json", {"github_repo": "example/stack"})
    code, _, err = _channel_cli(tmp_path, "../etc")
    assert code == 1 and "must name a branch" in err
    assert "channel" not in json.loads((tmp_path / "updates" / "config.json").read_text())


def test_asking_before_updates_are_enabled_says_so(tmp_path, monkeypatch):
    monkeypatch.delenv("JREMOTE_STATE_DIR", raising=False)
    code, _, err = _channel_cli(tmp_path)
    assert code == 1 and "updates enable" in err


@pytest.mark.parametrize("initial_state", ["pending", "current"])
def test_reused_request_keeps_its_job_after_state_and_inventory_change(tmp_path, release, initial_state):
    path = tmp_path / "fleet.sqlite"
    store = fleet.FleetStore(path)
    job = store.queue("leaf", "credential", release[2], "original")
    if initial_state == "current":
        for state in ("downloading", "applying", "verifying", "current"):
            store.transition(job["id"], "leaf", state, verified=True)
        store.report("leaf", {"verified": True, "release": "test-1"})
    assert store.queue("leaf", "credential", release[2], "update-all")["id"] == job["id"]
    if initial_state == "pending":
        store.transition(job["id"], "leaf", "failed")
    store.report("leaf", {"verified": False, "release": "old"})
    reopened = fleet.FleetStore(path)
    assert reopened.queue("leaf", "credential", release[2], "update-all")["id"] == job["id"]
    with pytest.raises(releases.ReleaseError, match="different update"):
        reopened.queue("leaf", "other-credential", release[2], "update-all")
