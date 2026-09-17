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


@pytest.mark.parametrize("mutation", ["missing", "skipped", "stale", "wrong_digest", "failed"])
def test_promotion_requires_exact_artifact_receipts(release, mutation):
    _, _, envelope = release
    manifest = envelope["manifest"]
    receipt = manifest["receipts"]["cellular"]
    if mutation == "missing":
        del manifest["receipts"]["cellular"]
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
    (evidence / "cellular.log").write_text("changed")
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
